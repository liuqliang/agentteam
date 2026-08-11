try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class GovernanceMixin:
    def test_p2_08_recovers_after_branch_advance_before_epoch_publish(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            _init_repo(repository)
            base = _git_head(repository)
            branch = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "symbolic-ref",
                    "--short",
                    "HEAD",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            work_root = root / "work"
            run_dir = work_root / "runs" / "promotion"
            frozen_dir = work_root / "frozen" / "promotion"
            run_dir.mkdir(parents=True)
            frozen_dir.mkdir(parents=True)
            declaration = _phase2_controller_gate_declarations()[0]
            context = {
                "work_root": work_root,
                "project_root": repository,
                "run_dir": run_dir,
                "frozen_dir": frozen_dir,
                "gate_root": run_dir / "state" / "gates",
                "epochs_root": run_dir / "state" / "gates" / "epochs",
                "declarations": [declaration],
                "declarations_by_id": {
                    "P2-08": declaration,
                },
            }
            current = {
                "record": {
                    "epoch_number": 1,
                    "integration_branch": branch,
                    "integration_head_sha": base,
                    "git_object_format": "sha1",
                },
                "digest": "1" * 64,
            }
            next_epoch = {
                "record": {
                    "epoch_number": 2,
                    "integration_head_sha": None,
                    "git_object_format": "sha1",
                },
                "digest": "2" * 64,
            }
            action_calls = []

            def execute_action(
                _input,
                action_context,
                **kwargs,
            ):
                action_calls.append(action_context["integration_head"])
                worktree = Path(
                    action_context["integration_worktree"]
                )
                readiness_path = (
                    worktree
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "m0_runtime"
                    / "agentteam_runtime"
                    / "data"
                    / "p0_experiment_readiness.v1.json"
                )
                readiness_path.parent.mkdir(parents=True)
                readiness_path.write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "add", "."],
                    cwd=worktree,
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Phase 2 Test",
                        "-c",
                        "user.email=phase2@example.invalid",
                        "commit",
                        "--quiet",
                        "-m",
                        "prepare R",
                    ],
                    cwd=worktree,
                    check=True,
                )
                head = _git_head(worktree)
                next_epoch["record"]["integration_head_sha"] = head
                artifact_path = run_dir / "acceptance" / "readiness.json"
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    '{"controller_validation_status":"passed"}\n',
                    encoding="utf-8",
                )
                return {
                    "gate_id": "P2-08",
                    "action_status": "completed",
                    "integration_head": head,
                    "artifact_path": str(artifact_path),
                    "artifact_sha256": hashlib.sha256(
                        artifact_path.read_bytes()
                    ).hexdigest(),
                    "relation_context": {
                        "protocol_path": str(
                            run_dir / "protocol.json"
                        ),
                    },
                }

            publish_calls = []

            def publish_epoch(
                _context,
                _current,
                result_head,
                **_kwargs,
            ):
                publish_calls.append(result_head)
                if len(publish_calls) == 1:
                    raise agentteam_module.Phase2GateError(
                        "injected epoch publication failure"
                    )
                return next_epoch

            with mock.patch.object(
                agentteam_module,
                "execute_readiness_promotion_action",
                side_effect=execute_action,
            ), mock.patch.object(
                agentteam_module,
                "_publish_phase2_action_epoch",
                side_effect=publish_epoch,
            ):
                with self.assertRaisesRegex(
                    agentteam_module.Phase2GateError,
                    "injected epoch",
                ):
                    agentteam_module._execute_phase2_controller_action(
                        context,
                        current,
                        declaration,
                        {},
                    )
                recovered = (
                    agentteam_module
                    ._execute_phase2_controller_action(
                        context,
                        current,
                        declaration,
                        {},
                    )
                )
            self.assertEqual(action_calls, [base])
            self.assertEqual(len(publish_calls), 2)
            self.assertTrue(recovered["epoch_refreshed"])
            self.assertEqual(_git_head(repository), recovered[
                "integration_head"
            ])
            self.assertTrue(
                (
                    context["epochs_root"]
                    / "2"
                    / "receipts"
                    / "P2-08.receipt.v1.json"
                ).is_file()
            )


    def test_p2_08_recovers_after_final_epoch_before_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            _init_repo(repository)
            base = _git_head(repository)
            readiness_path = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
                / "data"
                / "p0_experiment_readiness.v1.json"
            )
            readiness_path.parent.mkdir(parents=True)
            readiness_path.write_text("{}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "promote R"],
                cwd=repository,
                check=True,
            )
            readiness_head = _git_head(repository)
            for relative in (
                "experiments/native_agentteam_runtime/"
                "implementation_artifacts/reports/"
                "phase2-experiment-harness.md",
                "experiments/native_agentteam_runtime/"
                "implementation_artifacts/native_runtime_roadmap.md",
            ):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("finalized\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "finalize F"],
                cwd=repository,
                check=True,
            )
            final_head = _git_head(repository)
            branch = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "symbolic-ref",
                    "--short",
                    "HEAD",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            work_root = root / "work"
            run_dir = work_root / "runs" / "promotion"
            gate_root = run_dir / "state" / "gates"
            (gate_root / "actions").mkdir(parents=True)
            declaration = _phase2_controller_gate_declarations()[0]
            context = {
                "work_root": work_root,
                "project_root": repository,
                "run_dir": run_dir,
                "gate_root": gate_root,
                "epochs_root": gate_root / "epochs",
                "declarations_by_id": {"P2-08": declaration},
            }
            current = {
                "record": {
                    "epoch_number": 3,
                    "integration_branch": branch,
                    "integration_head_sha": final_head,
                    "git_object_format": "sha1",
                },
                "digest": "3" * 64,
            }
            base_epoch = {
                "epoch_number": 1,
                "integration_head_sha": base,
            }
            base_epoch_path = (
                gate_root / "epochs" / "1" / "epoch.v1.json"
            )
            _write_json(base_epoch_path, base_epoch)
            base_epoch_sha256 = agentteam_module._sha256_json(
                base_epoch
            )
            artifact_path = (
                run_dir / "acceptance" / "readiness.json"
            )
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(
                '{"controller_validation_status":"passed"}\n',
                encoding="utf-8",
            )
            action = {
                "gate_id": "P2-08",
                "action_status": "completed",
                "integration_head": readiness_head,
                "artifact_path": str(artifact_path),
                "artifact_sha256": hashlib.sha256(
                    artifact_path.read_bytes()
                ).hexdigest(),
                "relation_context": {
                    "protocol_path": str(
                        run_dir / "acceptance" / "protocol.json"
                    )
                },
            }
            _write_json(
                gate_root
                / "actions"
                / "P2-08.action-journal.v1.json",
                {
                    "schema_version": "phase2_action_journal.v1",
                    "gate_id": "P2-08",
                    "base_epoch_number": 1,
                    "base_epoch_sha256": base_epoch_sha256,
                    "base_integration_head": base,
                    "action_input_sha256": (
                        agentteam_module._sha256_json(
                            declaration["controller_action_input"]
                        )
                    ),
                    "result_integration_head": readiness_head,
                    "action": action,
                    "prepared_at": "2026-07-29T00:00:00Z",
                    "epoch_created_at": "2026-07-29T00:00:00Z",
                },
            )
            recovered = (
                agentteam_module._execute_phase2_controller_action(
                    context,
                    current,
                    declaration,
                    {},
                )
            )
            self.assertFalse(recovered["epoch_refreshed"])
            receipt = json.loads(
                (
                    gate_root
                    / "epochs"
                    / "3"
                    / "receipts"
                    / "P2-08.receipt.v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt["expected_integration_head_sha"],
                final_head,
            )
            artifact_path.write_text(
                '{"controller_validation_status":"tampered"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                agentteam_module.Phase2GateError,
                "journal evidence binding",
            ):
                agentteam_module._execute_phase2_controller_action(
                    context,
                    current,
                    declaration,
                    {},
                )


    def test_phase2_action_branch_advance_recovers_after_fast_forward(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            _init_repo(repository)
            base = _git_head(repository)
            branch = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "symbolic-ref",
                    "--short",
                    "HEAD",
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            action_worktree = root / "action"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(action_worktree),
                    base,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (action_worktree / "action.txt").write_text(
                "prepared\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "action.txt"],
                cwd=action_worktree,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Action Test",
                    "-c",
                    "user.email=action@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "prepared action",
                ],
                cwd=action_worktree,
                check=True,
            )
            result_head = _git_head(action_worktree)
            context = {"project_root": repository}
            current = {
                "record": {
                    "integration_branch": branch,
                    "integration_head_sha": base,
                }
            }
            first = agentteam_module._advance_phase2_action_branch(
                context,
                current,
                result_head=result_head,
            )
            second = agentteam_module._advance_phase2_action_branch(
                context,
                current,
                result_head=result_head,
            )
            self.assertEqual(first, repository.resolve())
            self.assertEqual(second, repository.resolve())
            self.assertEqual(_git_head(repository), result_head)


    def test_phase2_action_journal_is_idempotent_and_input_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _phase2_controller_gate_declarations()[0]
            context = {
                "gate_root": root / "gates",
            }
            current = {
                "record": {
                    "epoch_number": 1,
                    "integration_head_sha": "a" * 40,
                },
                "digest": "1" * 64,
            }
            action = {
                "gate_id": "P2-08",
                "action_status": "completed",
                "integration_head": "b" * 40,
                "relation_context": {"protocol_path": "/authority"},
            }
            first = agentteam_module._publish_phase2_action_journal(
                context,
                current,
                declaration,
                action,
            )
            second = agentteam_module._publish_phase2_action_journal(
                context,
                current,
                declaration,
                action,
            )
            self.assertEqual(first, second)
            drifted = copy.deepcopy(declaration)
            drifted["controller_action_input"]["configuration"][
                "promoted_at"
            ] = "2026-07-30T00:00:00Z"
            with self.assertRaisesRegex(
                agentteam_module.Phase2GateError,
                "journal conflicts",
            ):
                agentteam_module._publish_phase2_action_journal(
                    context,
                    current,
                    drifted,
                    action,
                )


    def test_phase2_action_receipts_keep_epoch_scoped_relation_contexts(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            run_dir = work_root / "runs" / "promotion"
            run_dir.mkdir(parents=True)
            declaration = _phase2_controller_gate_declarations()[0]
            context = {
                "work_root": work_root,
                "run_dir": run_dir,
                "epochs_root": work_root / "gates" / "epochs",
            }
            head = "a" * 40

            def current(number):
                return {
                    "record": {
                        "epoch_number": number,
                        "git_object_format": "sha1",
                    },
                    "digest": str(number) * 64,
                }

            first = agentteam_module._publish_phase2_action_receipt(
                context,
                current(2),
                declaration,
                integration_head=head,
                relation_context={"authority": "readiness-at-r"},
            )
            replay = agentteam_module._publish_phase2_action_receipt(
                context,
                current(2),
                declaration,
                integration_head=head,
                relation_context={"authority": "readiness-at-r"},
            )
            second = agentteam_module._publish_phase2_action_receipt(
                context,
                current(3),
                declaration,
                integration_head="b" * 40,
                relation_context={"authority": "readiness-at-f"},
            )
            self.assertEqual(first, replay)
            self.assertEqual(first["epoch_number"], 2)
            self.assertEqual(second["epoch_number"], 3)
            first_context = (
                run_dir
                / "state"
                / "phase2_gate_contexts"
                / "2"
                / "P2-08.relation-context.v1.json"
            )
            second_context = (
                run_dir
                / "state"
                / "phase2_gate_contexts"
                / "3"
                / "P2-08.relation-context.v1.json"
            )
            self.assertEqual(
                json.loads(first_context.read_text())["authority"],
                "readiness-at-r",
            )
            self.assertEqual(
                json.loads(second_context.read_text())["authority"],
                "readiness-at-f",
            )


    def test_registered_non_phase2_gate_uses_trusted_runtime_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            integration = root / "integration"
            evidence_run = root / "runs" / "readiness"
            integration.mkdir()
            evidence_run.mkdir(parents=True)
            current = {
                "record": {"epoch_number": 3},
                "digest": "3" * 64,
            }
            with mock.patch.object(
                agentteam_module,
                "_gate_integration_worktree",
                return_value=integration,
            ):
                relation = agentteam_module._gate_relation_context(
                    {},
                    current,
                    {"gate_id": "P3-READY"},
                    {
                        "P3-PRIOR": {
                            "state": "passed",
                            "evidence_sha256": "4" * 64,
                            "validated_code_sha": "a" * 40,
                        }
                    },
                    evidence_run,
                    "b" * 40,
                )
            self.assertEqual(relation["epoch_number"], 3)
            self.assertEqual(relation["integration_head"], "b" * 40)
            self.assertEqual(
                relation["repository_root"], str(integration)
            )
            self.assertEqual(
                relation["evidence_run"], str(evidence_run.resolve())
            )
            self.assertEqual(
                relation["prior_gate_evidence"],
                {"P3-PRIOR": "4" * 64},
            )


    def test_decision_aware_blueprint_materializes_and_freezes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            contract = _blueprint_decision_contract(
                [task["task_id"] for task in blueprint["tasks"]]
            )
            blueprint["agents"][0]["runtime_profile"].update(
                {
                    "model": "gpt-5.6-sol",
                    "reasoning_profile": "medium",
                }
            )
            blueprint["decision_contract"] = contract
            blueprint["approval"]["digest_bindings"].append(
                "decision_contract"
            )
            _write_json(repo / blueprint_path, blueprint)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "bind blueprint decisions"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _write_blueprint_approval(repo, blueprint)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "approve blueprint decisions"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                work_root / "drafts",
            )
            draft = load_taskpack(materialized["taskpack_dir"])["taskpack"]
            materialized_agent_pool = load_taskpack(
                materialized["taskpack_dir"]
            )["agent_pool"]
            self.assertEqual(draft["decision_contract"], contract)
            self.assertEqual(
                materialized_agent_pool["role_runtime_profiles"][
                    "implementation_worker"
                ]["reasoning_profile"],
                "medium",
            )
            self.assertEqual(
                materialized["decision_contract_sha256"],
                taskpack_module._sha256_json(contract),
            )
            self.assertEqual(
                materialized["root_decision_id"],
                contract["root_decision_id"],
            )

            frozen = freeze_taskpack(
                materialized["taskpack_dir"],
                work_root / "frozen",
            )
            frozen_taskpack = load_taskpack(
                frozen["frozen_taskpack_dir"]
            )["taskpack"]
            self.assertEqual(frozen_taskpack["decision_contract"], contract)
            self.assertEqual(
                taskpack_module.verify_frozen_taskpack_digest(
                    frozen["frozen_taskpack_dir"],
                    frozen["manifest"]["digest_sha256"],
                )["digest_sha256"],
                frozen["manifest"]["digest_sha256"],
            )
            run_dir = work_root / "runs" / "example-blueprint"
            first_binding = publish_run_decision_binding(
                work_root,
                frozen["frozen_taskpack_dir"],
                run_dir,
                frozen_taskpack,
                task_ids=["T-1", "T-2", "T-3"],
            )
            replayed_binding = publish_run_decision_binding(
                work_root,
                frozen["frozen_taskpack_dir"],
                run_dir,
                frozen_taskpack,
                task_ids=["T-1", "T-2", "T-3"],
            )
            self.assertEqual(replayed_binding, first_binding)
            self.assertEqual(
                load_run_decision_binding(run_dir),
                first_binding,
            )


    def test_decision_aware_blueprint_requires_complete_execution_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            contract = _blueprint_decision_contract(["T-1", "T-2"])
            blueprint["decision_contract"] = contract
            blueprint["approval"]["digest_bindings"].append(
                "decision_contract"
            )
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "decision-aware blueprint must bind every task: T-3",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "unused",
                    dry_run=True,
                )

            contract["task_bindings"]["T-3"] = contract[
                "root_decision_id"
            ]
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "tasks must bind execution decisions: T-3",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "unused",
                    dry_run=True,
                )


    def test_decision_aware_blueprint_requires_approval_digest_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["decision_contract"] = _blueprint_decision_contract(
                [task["task_id"] for task in blueprint["tasks"]]
            )
            _write_json(repo / blueprint_path, blueprint)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "approval must bind decision_contract",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "unused",
                    dry_run=True,
                )


    def test_decision_aware_blueprint_rejects_approval_digest_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["decision_contract"] = _blueprint_decision_contract(
                [task["task_id"] for task in blueprint["tasks"]]
            )
            blueprint["approval"]["digest_bindings"].append(
                "decision_contract"
            )
            _write_json(repo / blueprint_path, blueprint)
            record = _write_blueprint_approval(repo, blueprint)
            record["decision_contract_sha256"] = "0" * 64
            _write_json(repo / blueprint["approval"]["record_path"], record)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "approval digest mismatch for decision_contract",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "unused",
                )


    def test_blueprint_materialization_rejects_committed_gate_schema_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            schema_path = repo / blueprint["post_backlog_gates"][0]["evidence_schema"]
            _write_json(schema_path, {"type": "object"})
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "track gate schema"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _write_json(schema_path, {"type": "string"})

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "FINAL.evidence_schema must match the committed HEAD bytes",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "drafts",
                )


    def test_tracked_phase2_blueprint_dry_run_has_exact_task_and_edge_counts(self):
        project_root = Path(__file__).resolve().parents[4]
        blueprint_path = (
            "experiments/native_agentteam_runtime/implementation_artifacts/plans/"
            "2026-07-27-phase2-experiment-harness-and-calibration.blueprint.json"
        )

        result = taskpack_module.materialize_taskpack_blueprint(
            project_root,
            blueprint_path,
            Path(tempfile.gettempdir()) / "unused-phase2-blueprint-output",
            dry_run=True,
        )

        self.assertEqual(
            result["task_ids"],
            [
                "P2-MAP",
                "P2-01",
                "P2-02A",
                "P2-02B",
                "P2-03A",
                "P2-03B",
                "P2-04",
                "P2-05",
                "P2-06",
                "P2-07A",
                "P2-07B",
            ],
        )
        self.assertEqual(result["task_count"], 11)
        self.assertEqual(result["dependency_edge_count"], 11)
        self.assertEqual(result["validation_status"], "accepted")
        self.assertFalse(result["freeze_eligible"])


    def test_completion_summary_includes_review_gate_guidance_for_integrate_action(self):
        summary = build_completion_summary(
            run_id="taskpack-7",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "optimize-pipeline",
                    "status": "implementation completed",
                    "what_changed": ["优化了手势评分流水线。"],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                }
            ],
            integration_baseline={
                "branch": "agentteam/run/taskpack-7/integration",
                "worktree_path": "/tmp/taskpack-7/integration-baseline",
                "base_sha": "base123",
                "head_sha": "abc123",
            },
        )

        self.assertEqual(summary["follow_up_recommendation"]["action"], "integrate")
        self.assertEqual(
            summary["review_gate"],
            {
                "status": "review_gate_required",
                "integration_branch": "agentteam/run/taskpack-7/integration",
                "base_head": "base123",
                "baseline_head": "abc123",
                "integration_worktree": "/tmp/taskpack-7/integration-baseline",
                "report_command": "agentteam report --taskpack taskpack-7",
                "paths_command": "agentteam paths --taskpack taskpack-7",
                "diff_command": "git -C /tmp/taskpack-7/integration-baseline diff --stat base123..abc123",
                "integrate_command": "agentteam integrate --taskpack taskpack-7",
                "operator_note": (
                    "Review report, paths, and diff before integrating; source merge, "
                    "push, and release activation remain operator decisions."
                ),
            },
        )
        lines = []
        extend_completion_summary_lines(lines, summary)
        self.assertIn("Review gate:", lines)
        self.assertIn("- status: review_gate_required", lines)
        self.assertIn("- report_command: agentteam report --taskpack taskpack-7", lines)
        self.assertIn(
            "- diff_command: git -C /tmp/taskpack-7/integration-baseline diff --stat base123..abc123",
            lines,
        )


    def test_concise_report_lines_aggregate_multiple_tasks(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 2,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["实现任务 A 的代码路径。", "补充任务 B 的验证入口。"],
                    "changed_files": ["src/a.py", "src/b.py"],
                    "verification": ["unit-a: passed", "unit-b: passed"],
                    "integration": "passed",
                    "next_steps": ["继续验证 A。", "继续验证 B。"],
                },
                "operator_report": {
                    "task_reports": [
                        {
                            "task_id": "TASK-A",
                            "status": "implementation completed",
                            "what_changed": ["实现任务 A 的代码路径。"],
                            "next_steps": ["继续验证 A。"],
                        },
                        {
                            "task_id": "TASK-B",
                            "status": "implementation completed",
                            "what_changed": ["补充任务 B 的验证入口。"],
                            "next_steps": ["继续验证 B。"],
                        },
                    ]
                },
            }
        )

        self.assertIn("changed: 实现任务 A 的代码路径。；补充任务 B 的验证入口。", lines)
        self.assertIn("changed_files: src/a.py；src/b.py", lines)
        self.assertIn("verification: unit-a: passed；unit-b: passed", lines)
        self.assertIn("next: 继续验证 A。；继续验证 B。", lines)
        self.assertIn("task TASK-A: implementation completed", lines)
        self.assertIn("task TASK-B: implementation completed", lines)


    def test_concise_report_lines_do_not_require_review_gate_for_non_agentteam_target(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "token_usage": {"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
                "completion_summary": {
                    "chinese_operator_brief": ["本次运行已完成，共 1 个任务，0 个阻塞。"],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "follow_up_recommendation": {
                        "action": "review_report",
                        "report_command": "agentteam report --taskpack taskpack-7",
                    },
                },
                "operator_report": {
                    "task_reports": [
                        {
                            "task_id": "TASK-NON-AGENTTEAM-001",
                            "status": "implementation completed",
                        }
                    ]
                },
            }
        )

        self.assertFalse(
            any(line.startswith("report_completeness:") for line in lines),
            lines,
        )


    def test_concise_report_lines_require_review_gate_for_agentteam_target(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "token_usage": {"total_tokens": 10, "input_tokens": 8, "output_tokens": 2},
                "completion_summary": {
                    "chinese_operator_brief": ["本次运行已完成，共 1 个任务，0 个阻塞。"],
                    "changed_files": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py"],
                    "verification": ["unit_tests: passed"],
                    "follow_up_recommendation": {
                        "action": "review_report",
                        "report_command": "agentteam report --taskpack taskpack-7",
                    },
                },
                "operator_report": {
                    "task_reports": [
                        {
                            "task_id": "TASK-AGENTTEAM-001",
                            "status": "implementation completed",
                            "agentteam_target_review_required": True,
                        }
                    ]
                },
            }
        )

        self.assertIn("report_completeness: missing=review_gate", lines)


    def test_gate_schema_from_git_resolves_only_committed_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            child_id = "https://agentteam.local/schemas/child.schema.json"
            _write_json(
                repo / "schemas" / "child.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": child_id,
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                    "additionalProperties": False,
                },
            )
            _write_json(
                repo / "schemas" / "root.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://agentteam.local/schemas/root.schema.json",
                    "type": "object",
                    "required": ["child"],
                    "properties": {"child": {"$ref": child_id}},
                    "additionalProperties": False,
                },
            )
            subprocess.run(["git", "add", "schemas"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add referenced schemas"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            head = _git_head(repo)

            with mock.patch(
                "requests.get",
                side_effect=AssertionError("schema validation attempted network"),
            ):
                digest = agentteam_module._validate_schema_from_git(
                    repo,
                    head,
                    "schemas/root.schema.json",
                    {"child": {"value": 1}},
                )

            self.assertRegex(digest, r"^[0-9a-f]{64}$")


    def test_gate_schema_from_git_rejects_uncommitted_external_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            _write_json(
                repo / "schemas" / "root.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://agentteam.local/schemas/root.schema.json",
                    "$ref": "https://example.invalid/missing.schema.json",
                },
            )
            subprocess.run(["git", "add", "schemas"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add unresolved schema"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            with mock.patch(
                "requests.get",
                side_effect=AssertionError("schema validation attempted network"),
            ):
                with self.assertRaisesRegex(
                    agentteam_module.AgentTeamCliError,
                    "committed gate schema reference is unavailable",
                ):
                    agentteam_module._validate_schema_from_git(
                        repo,
                        _git_head(repo),
                        "schemas/root.schema.json",
                        {},
                    )


    def test_post_backlog_gate_seal_register_and_integrate_enforcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            evidence_schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["controller_validation_status", "validated_code_sha"],
                "properties": {
                    "controller_validation_status": {"const": "passed"},
                    "validated_code_sha": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            }
            _write_json(repo / "schemas" / "live.schema.json", evidence_schema)
            _write_json(
                repo / "schemas" / "final.schema.json",
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "controller_validation_status",
                        "validated_code_sha",
                        "final_report_sha",
                        "changed_paths",
                    ],
                    "properties": {
                        "controller_validation_status": {"const": "passed"},
                        "validated_code_sha": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{40}$",
                        },
                        "final_report_sha": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{40}$",
                        },
                        "changed_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            )
            approval_schema_path = (
                REPO_ROOT
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
                / "post_backlog_gate_approval.schema.json"
            )
            _write_json(
                repo / "schemas" / "approval.schema.json",
                json.loads(approval_schema_path.read_text(encoding="utf-8")),
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add live gate schema"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _init_agentteam_profile_for_test(repo, work_root, "gated-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a gated integration fixture.",
                "gated-integrate-run",
            )
            frozen_taskpack_path = (
                work_root / "frozen" / "gated-integrate-run" / "taskpack.yaml"
            )
            taskpack = json.loads(frozen_taskpack_path.read_text(encoding="utf-8"))
            taskpack["post_backlog_gates"] = [
                {
                    "gate_id": "P1-LIVE",
                    "depends_on": [],
                    "executor": "deterministic_controller",
                    "evidence_run_registration_required": True,
                    "evidence_artifact": "acceptance/live.v1.json",
                    "evidence_schema": "schemas/live.schema.json",
                    "required_status_field": "controller_validation_status",
                    "required_status_value": "passed",
                    "commit_field": "validated_code_sha",
                    "integration_head_relation": "ancestor_of",
                },
                {
                    "gate_id": "P1-06E",
                    "depends_on": ["P1-LIVE"],
                    "executor": "deterministic_controller",
                    "operator_review_required": True,
                    "operator_approval_schema": "schemas/approval.schema.json",
                    "operator_approval_required_decision": "approved",
                    "evidence_run_registration_required": True,
                    "evidence_artifact": "acceptance/final.v1.json",
                    "evidence_schema": "schemas/final.schema.json",
                    "required_status_field": "controller_validation_status",
                    "required_status_value": "passed",
                    "commit_field": "final_report_sha",
                    "integration_head_relation": "equals",
                },
            ]
            _write_json(frozen_taskpack_path, taskpack)
            frozen_verification_path = (
                work_root / "frozen" / "gated-integrate-run" / "verification.json"
            )
            frozen_verification = json.loads(
                frozen_verification_path.read_text(encoding="utf-8")
            )
            frozen_verification["command"] = ["python3", "-c", "pass"]
            _write_json(frozen_verification_path, frozen_verification)
            run_dir = work_root / "runs" / "gated-integrate-run"
            baseline_state = json.loads(
                (run_dir / "state" / "two_phase_scheduler_state.json").read_text(
                    encoding="utf-8"
                )
            )
            baseline_branch = baseline_state["integration_baseline"][
                "integration_baseline_branch"
            ]
            historical_baseline_head = baseline_state["integration_baseline"][
                "integration_baseline_head_sha"
            ]
            baseline_worktree = run_dir / "integration-baseline"
            (baseline_worktree / "refresh-conflict.txt").write_text(
                "integration\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "refresh-conflict.txt"],
                cwd=baseline_worktree,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add refresh conflict fixture"],
                cwd=baseline_worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            baseline_head = subprocess.run(
                ["git", "rev-parse", baseline_branch],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            blocked = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--record-only",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("required post-backlog gates are not passed", blocked.stderr)
            state_after_block = json.loads(
                (run_dir / "state" / "two_phase_scheduler_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotEqual(
                state_after_block["integration_baseline"].get(
                    "integration_baseline_status"
                ),
                "acknowledged",
            )

            status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            status_payload = json.loads(status.stdout)
            self.assertIn("gate seal-baseline", status_payload["next_action"])
            self.assertNotIn("agentteam integrate", status_payload["next_action"])
            report = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            report_payload = json.loads(report.stdout)
            self.assertEqual(
                report_payload["run_status"],
                "awaiting_post_backlog_gates",
            )
            self.assertEqual(
                report_payload["completion_summary"]["review_gate"]["status"],
                "post_backlog_gates_pending",
            )
            self.assertNotIn(
                "agentteam integrate",
                json.dumps(report_payload["completion_summary"]),
            )

            profile = agentteam_module.load_project_profile(repo)
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "fully done with no current integration block",
            ):
                agentteam_module._gate_seal_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_integration_head=baseline_head,
                )
            verified_idle = {
                "status": "idle",
                "tasks": {"total": 1, "done": 1, "blocked": 0, "ready": 0},
                "integration": {"total": 1, "blocked": 0, "verified": 1},
            }
            original_git_stdout = agentteam_module._git_stdout
            branch_reads = 0

            def changed_head_after_verification(repo_path, command):
                nonlocal branch_reads
                if command == [
                    "rev-parse",
                    "--verify",
                    f"{baseline_branch}^{{commit}}",
                ]:
                    branch_reads += 1
                    if branch_reads == 2:
                        return "f" * 40
                return original_git_stdout(repo_path, command)

            with mock.patch.object(
                agentteam_module,
                "_build_run_status_summary",
                return_value=verified_idle,
            ):
                with mock.patch.object(
                    agentteam_module,
                    "_git_stdout",
                    side_effect=changed_head_after_verification,
                ):
                    with self.assertRaisesRegex(
                        agentteam_module.AgentTeamCliError,
                        "changed during frozen verification",
                    ):
                        agentteam_module._gate_seal_baseline(
                            repo,
                            profile,
                            run_dir,
                            expected_integration_head=baseline_head,
                        )
            self.assertFalse(
                (run_dir / "state" / "post_backlog_gates" / "epochs" / "1").exists()
            )
            with mock.patch.object(
                agentteam_module,
                "_build_run_status_summary",
                return_value=verified_idle,
            ):
                sealed = agentteam_module._gate_seal_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_integration_head=baseline_head,
                )
            self.assertEqual(sealed["gate_epoch"], 1)
            epoch_path = (
                run_dir
                / "state"
                / "post_backlog_gates"
                / "epochs"
                / "1"
                / "epoch.v1.json"
            )
            self.assertTrue(epoch_path.is_file())
            original_execution_mode = taskpack.get("execution_mode")
            tampered_taskpack = json.loads(
                frozen_taskpack_path.read_text(encoding="utf-8")
            )
            tampered_taskpack["execution_mode"] = "controller_only"
            _write_json(frozen_taskpack_path, tampered_taskpack)
            tampered_context = (
                agentteam_module._post_backlog_gate_context(
                    profile,
                    run_dir,
                )
            )
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "gate epoch declaration digest is stale",
            ):
                agentteam_module._read_current_gate_epoch(
                    tampered_context
                )
            if original_execution_mode is None:
                tampered_taskpack.pop("execution_mode", None)
            else:
                tampered_taskpack["execution_mode"] = (
                    original_execution_mode
                )
            _write_json(frozen_taskpack_path, tampered_taskpack)

            evidence_run = work_root / "runs" / "live-evidence-run"
            _write_json(
                evidence_run / "acceptance" / "live.v1.json",
                {
                    "controller_validation_status": "passed",
                    "validated_code_sha": baseline_head,
                },
            )
            registered = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gate",
                    "register",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--gate",
                    "P1-LIVE",
                    "--gate-epoch",
                    "1",
                    "--evidence-run",
                    "live-evidence-run",
                    "--expected-integration-head",
                    baseline_head,
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(registered.returncode, 0, registered.stderr)

            receipt_path = (
                run_dir
                / "state"
                / "post_backlog_gates"
                / "epochs"
                / "1"
                / "receipts"
                / "P1-LIVE.receipt.v1.json"
            )
            original_receipt = receipt_path.read_bytes()
            tampered_receipt = json.loads(original_receipt)
            tampered_receipt["epoch_sha256"] = "0" * 64
            _write_json(receipt_path, tampered_receipt)
            tampered_status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            tampered_payload = json.loads(tampered_status.stdout)
            self.assertEqual(
                tampered_payload["post_backlog_gates"]["gates"][0]["state"],
                "failed",
            )
            self.assertEqual(
                tampered_payload["status"],
                "awaiting_post_backlog_gates",
            )
            receipt_path.write_bytes(original_receipt)

            artifact_path = evidence_run / "acceptance" / "live.v1.json"
            valid_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            _write_json(
                artifact_path,
                {
                    **valid_artifact,
                    "controller_validation_status": "failed",
                },
            )
            failed_artifact_status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertEqual(
                json.loads(failed_artifact_status.stdout)["post_backlog_gates"][
                    "gates"
                ][0]["state"],
                "failed",
            )
            _write_json(artifact_path, valid_artifact)

            baseline_schema_path = (
                run_dir / "integration-baseline" / "schemas" / "live.schema.json"
            )
            baseline_schema_bytes = baseline_schema_path.read_bytes()
            baseline_schema_path.write_bytes(baseline_schema_bytes + b"\n")
            dirty_schema_status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertEqual(
                json.loads(dirty_schema_status.stdout)["post_backlog_gates"]["gates"][
                    0
                ]["state"],
                "failed",
            )
            baseline_schema_path.write_bytes(baseline_schema_bytes)

            final_evidence_run = work_root / "runs" / "final-evidence-run"
            final_evidence_run.mkdir(parents=True)
            final_registered = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gate",
                    "register",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--gate",
                    "P1-06E",
                    "--gate-epoch",
                    "1",
                    "--evidence-run",
                    "final-evidence-run",
                    "--expected-integration-head",
                    baseline_head,
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(final_registered.returncode, 0, final_registered.stderr)
            report_paths = [
                "experiments/native_agentteam_runtime/implementation_artifacts/"
                "reports/phase1-model-invocation-usage.md",
                "experiments/native_agentteam_runtime/implementation_artifacts/"
                "native_runtime_roadmap.md",
            ]
            for relative_path in report_paths:
                output_path = baseline_worktree / relative_path
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(f"fixture for {relative_path}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", *report_paths],
                cwd=baseline_worktree,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add final report-only fixture"],
                cwd=baseline_worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            final_report_head = _git_head(baseline_worktree)
            final_artifact_path = (
                final_evidence_run / "acceptance" / "final.v1.json"
            )
            _write_json(
                final_artifact_path,
                {
                    "controller_validation_status": "passed",
                    "validated_code_sha": baseline_head,
                    "final_report_sha": final_report_head,
                    "changed_paths": report_paths,
                },
            )
            awaiting_review = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            awaiting_payload = json.loads(awaiting_review.stdout)
            self.assertEqual(
                awaiting_payload["status"],
                "awaiting_post_backlog_gates",
            )
            self.assertEqual(
                awaiting_payload["post_backlog_gates"]["gates"][1]["state"],
                "awaiting_operator_review",
            )
            self.assertIn("gate approve", awaiting_payload["next_action"])
            evidence_sha256 = hashlib.sha256(final_artifact_path.read_bytes()).hexdigest()
            self.assertEqual(
                awaiting_payload["integration_baseline"]["head_sha"],
                final_report_head,
            )
            self.assertEqual(
                awaiting_payload["integration_baseline"][
                    "historical_scheduler_head_sha"
                ],
                historical_baseline_head,
            )
            self.assertEqual(
                sorted(
                    awaiting_payload["operator_review"]["review_gate"][
                        "changed_paths"
                    ],
                ),
                sorted(report_paths),
            )
            self.assertEqual(
                awaiting_payload["operator_review"]["review_gate"][
                    "integration_head_relation"
                ],
                "equals",
            )
            self.assertIn(
                f"--expected-evidence-sha256 {evidence_sha256}",
                awaiting_payload["operator_review"]["approval_command"],
            )
            self.assertIn(
                f"--expected-integration-head {final_report_head}",
                awaiting_payload["operator_review"]["approval_command"],
            )

            fresh_paths = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            fresh_paths_payload = json.loads(fresh_paths.stdout)
            self.assertEqual(
                fresh_paths_payload["integration_baseline"]["head_sha"],
                final_report_head,
            )
            self.assertIn(
                f"{baseline_head}..{final_report_head}",
                fresh_paths_payload["review_commands"]["diff"],
            )
            self.assertEqual(
                sorted(
                    fresh_paths_payload["operator_review"]["review_gate"][
                        "changed_paths"
                    ],
                ),
                sorted(report_paths),
            )
            self.assertNotIn(
                "integrate",
                fresh_paths_payload["review_commands"],
            )

            fresh_report = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            fresh_report_payload = json.loads(fresh_report.stdout)
            fresh_review_gate = fresh_report_payload["completion_summary"][
                "review_gate"
            ]
            self.assertEqual(
                fresh_report_payload["integration_baseline"]["head_sha"],
                final_report_head,
            )
            self.assertEqual(fresh_review_gate["baseline_head"], final_report_head)
            self.assertEqual(
                sorted(fresh_review_gate["report_paths"]),
                sorted(report_paths),
            )
            self.assertEqual(fresh_review_gate["gate_id"], "P1-06E")
            self.assertEqual(fresh_review_gate["integration_head_relation"], "equals")
            self.assertEqual(
                fresh_review_gate["approval_command"],
                awaiting_payload["operator_review"]["approval_command"],
            )
            profile = agentteam_module.load_project_profile(repo)
            confirmation = "approve gated-integrate-run P1-06E epoch 1\n"
            with mock.patch.object(
                agentteam_module,
                "_require_operator_approval_context",
                return_value=None,
            ):
                with mock.patch.object(sys, "stdin", io.StringIO(confirmation)):
                    approved = agentteam_module._gate_approve(
                        repo,
                        profile,
                        run_dir,
                        gate_id="P1-06E",
                        gate_epoch=1,
                        expected_evidence_sha256=evidence_sha256,
                        expected_integration_head=final_report_head,
                    )
            self.assertEqual(approved["gate_status"], "passed")
            self.assertEqual(approved["run_completion"]["run_status"], "completed")
            self.assertTrue(approved["run_completion"]["idempotent"])
            run_events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(event["event_type"] == "run_completed" for event in run_events),
                1,
            )
            approved_status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            approved_payload = json.loads(approved_status.stdout)
            self.assertIn(
                "(uid=",
                approved_payload["operator_review"]["validated_approval"][
                    "operator_identity"
                ],
            )
            self.assertEqual(
                approved_payload["operator_review"]["integration_head_sha"],
                final_report_head,
            )
            with mock.patch.object(
                agentteam_module,
                "_require_operator_approval_context",
                return_value=None,
            ):
                with mock.patch.object(sys, "stdin", io.StringIO(confirmation)):
                    replayed_approval = agentteam_module._gate_approve(
                        repo,
                        profile,
                        run_dir,
                        gate_id="P1-06E",
                        gate_epoch=1,
                        expected_evidence_sha256=evidence_sha256,
                        expected_integration_head=final_report_head,
                    )
            self.assertTrue(replayed_approval["idempotent"])
            self.assertTrue(replayed_approval["run_completion"]["idempotent"])

            completed_receipt = receipt_path.read_bytes()
            stale_receipt = json.loads(completed_receipt)
            stale_receipt["epoch_sha256"] = "0" * 64
            _write_json(receipt_path, stale_receipt)
            stale_after_completion = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            stale_after_completion_payload = json.loads(stale_after_completion.stdout)
            self.assertEqual(
                stale_after_completion_payload["status"],
                "awaiting_post_backlog_gates",
            )
            self.assertEqual(
                stale_after_completion_payload["post_backlog_gates"]["gates"][0][
                    "state"
                ],
                "failed",
            )
            events_after_stale_status = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                sum(
                    event["event_type"] == "run_completed"
                    for event in events_after_stale_status
                ),
                1,
            )
            receipt_path.write_bytes(completed_receipt)

            rebased = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--rebase",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(rebased.returncode, 0)
            self.assertIn("rebase is forbidden", rebased.stderr)
            self.assertEqual(_git_head(repo), historical_baseline_head)

            integrated = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--record-only",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(integrated.returncode, 0, integrated.stderr)
            integrated_payload = json.loads(integrated.stdout)
            self.assertEqual(integrated_payload["integrate_status"], "acknowledged")
            self.assertEqual(
                integrated_payload["integration_baseline"]["authority"],
                "current_gate_epoch_git_ref",
            )
            self.assertEqual(
                integrated_payload["integration_baseline"]["head_sha"],
                final_report_head,
            )
            self.assertEqual(
                integrated_payload["integration_baseline"][
                    "historical_scheduler_head_sha"
                ],
                historical_baseline_head,
            )

            epoch_one_receipt = receipt_path.read_bytes()
            epoch_one_approval_path = Path(approved["path"])
            epoch_one_approval = epoch_one_approval_path.read_bytes()
            cost_history_path = (
                work_root / "runs" / "gate-controller-costs" / "cost_history.json"
            )
            _write_json(
                cost_history_path,
                {
                    "implementation_run_id": "gated-integrate-run",
                    "gate_epoch": 1,
                    "provider_cost_usd": 1.25,
                },
            )
            cost_history = cost_history_path.read_bytes()

            # Regression 3: a merge conflict leaves epoch 1 and all evidence intact.
            (repo / "refresh-conflict.txt").write_text("target\n", encoding="utf-8")
            subprocess.run(["git", "add", "refresh-conflict.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "create refresh conflict"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            conflicting_target_head = _git_head(repo)
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "target merge conflicted",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=1,
                    expected_target_head=conflicting_target_head,
                )
            gate_context = agentteam_module._post_backlog_gate_context(
                profile,
                run_dir,
            )
            self.assertEqual(
                agentteam_module._read_current_gate_epoch(gate_context)["record"][
                    "epoch_number"
                ],
                1,
            )
            self.assertFalse((run_dir / "integration-epoch-2").exists())
            self.assertNotEqual(
                subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{baseline_branch}-epoch-2"],
                    cwd=repo,
                    check=False,
                ).returncode,
                0,
            )

            # Reconcile the target and exercise the exact command contract.
            (repo / "refresh-conflict.txt").write_text(
                "integration\n",
                encoding="utf-8",
            )
            (repo / "target-refresh-1.txt").write_text("target epoch 2\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "refresh-conflict.txt", "target-refresh-1.txt"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "advance target for epoch 2"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            target_head_epoch_two = _git_head(repo)
            refreshed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gate",
                    "refresh-baseline",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "gated-integrate-run",
                    "--expected-gate-epoch",
                    "1",
                    "--expected-target-head",
                    target_head_epoch_two,
                    "--authorize-revalidation",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
            refreshed_payload = json.loads(refreshed.stdout)
            self.assertEqual(refreshed_payload["gate_epoch"], 2)
            self.assertEqual(refreshed_payload["parent_gate_epoch"], 1)
            self.assertIn("-epoch-2", refreshed_payload["integration_branch"])
            epoch_two = agentteam_module._read_current_gate_epoch(gate_context)
            self.assertEqual(epoch_two["record"]["prior_epoch_sha256"], sealed["epoch_sha256"])
            self.assertNotEqual(
                epoch_two["record"]["validated_code_sha"],
                final_report_head,
            )
            self.assertEqual(
                epoch_two["record"]["validated_code_sha"],
                refreshed_payload["validated_code_sha"],
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "rev-parse",
                        f"{refreshed_payload['validated_code_sha']}^1",
                    ],
                    cwd=repo,
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                baseline_head,
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        target_head_epoch_two,
                        refreshed_payload["validated_code_sha"],
                    ],
                    cwd=repo,
                    check=False,
                ).returncode,
                0,
            )
            epoch_two_decision = agentteam_module._evaluate_post_backlog_gates(
                gate_context,
                current=epoch_two,
            )
            self.assertFalse(epoch_two_decision["all_passed"])
            self.assertTrue(
                all(gate["state"] == "pending" for gate in epoch_two_decision["gates"])
            )
            self.assertEqual(receipt_path.read_bytes(), epoch_one_receipt)
            self.assertEqual(epoch_one_approval_path.read_bytes(), epoch_one_approval)

            (repo / "target-refresh-2.txt").write_text("target epoch 3\n", encoding="utf-8")
            subprocess.run(["git", "add", "target-refresh-2.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance target for epoch 3"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            target_head_epoch_three = _git_head(repo)

            # Regressions 3/5: stale refs and open invocations fail in-lock.
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "expected target head changed",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=2,
                    expected_target_head=target_head_epoch_two,
                )
            controller_state = (
                work_root
                / "runs"
                / "refresh-controller"
                / "state"
                / "gate_controller_invocation.json"
            )
            _write_json(
                controller_state,
                {
                    "implementation_run_id": "gated-integrate-run",
                    "gate_id": "P1-LIVE",
                    "gate_epoch": 2,
                    "invocation_id": "refresh-open-1",
                    "status": "running",
                },
            )
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "open gate controller invocation",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=2,
                    expected_target_head=target_head_epoch_three,
                )
            controller_state.unlink()

            # A pre-existing candidate is not proven to belong to this
            # invocation and must never be deleted as automatic cleanup.
            epoch_three_branch = (
                f"{baseline_branch}-epoch-3"
            )
            subprocess.run(
                [
                    "git",
                    "branch",
                    epoch_three_branch,
                    epoch_two["record"]["validated_code_sha"],
                ],
                cwd=repo,
                check=True,
            )
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "refusing destructive cleanup",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=2,
                    expected_target_head=target_head_epoch_three,
                )
            self.assertEqual(
                subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{epoch_three_branch}"],
                    cwd=repo,
                    check=False,
                ).returncode,
                0,
            )
            subprocess.run(
                ["git", "branch", "-D", epoch_three_branch],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Regression 4: the run/gate locks serialize concurrent refreshes.
            with agentteam_module._gate_mutation_locks(
                gate_context,
                sorted(gate_context["declarations_by_id"]),
            ):
                with self.assertRaisesRegex(
                    agentteam_module.AgentTeamCliError,
                    "post-backlog gate mutation is active",
                ):
                    agentteam_module._gate_refresh_baseline(
                        repo,
                        profile,
                        run_dir,
                        expected_gate_epoch=2,
                        expected_target_head=target_head_epoch_three,
                    )

            # Regression 5: a worktree change during verification is caught by
            # the mandatory post-verification in-lock reread.
            target_dirty_path = repo / "dirty-during-refresh.txt"
            frozen_verification["command"] = [
                "python3",
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(target_dirty_path)!r}).write_text('dirty\\n')"
                ),
            ]
            _write_json(frozen_verification_path, frozen_verification)
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "target worktree changed during baseline refresh",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=2,
                    expected_target_head=target_head_epoch_three,
                )
            target_dirty_path.unlink()
            self.assertFalse((run_dir / "integration-epoch-3").exists())

            # Regression 3: verification failure and pre-publication crash clean up.
            frozen_verification["command"] = ["python3", "-c", "raise SystemExit(9)"]
            _write_json(frozen_verification_path, frozen_verification)
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "frozen full verification failed",
            ):
                agentteam_module._gate_refresh_baseline(
                    repo,
                    profile,
                    run_dir,
                    expected_gate_epoch=2,
                    expected_target_head=target_head_epoch_three,
                )
            frozen_verification["command"] = ["python3", "-c", "pass"]
            _write_json(frozen_verification_path, frozen_verification)
            with mock.patch.object(
                agentteam_module,
                "_publish_gate_epoch",
                side_effect=RuntimeError("simulated pre-publication crash"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated pre-publication crash",
                ):
                    agentteam_module._gate_refresh_baseline(
                        repo,
                        profile,
                        run_dir,
                        expected_gate_epoch=2,
                        expected_target_head=target_head_epoch_three,
                    )
            self.assertEqual(
                agentteam_module._read_current_gate_epoch(gate_context)["record"][
                    "epoch_number"
                ],
                2,
            )
            self.assertFalse((run_dir / "integration-epoch-3").exists())

            # Regressions 6/7/8: old costs stay queryable, stale epoch mutation
            # fails, and the same protocol publishes epoch N+2.
            refreshed_again = agentteam_module._gate_refresh_baseline(
                repo,
                profile,
                run_dir,
                expected_gate_epoch=2,
                expected_target_head=target_head_epoch_three,
            )
            self.assertEqual(refreshed_again["gate_epoch"], 3)
            epoch_three = agentteam_module._read_current_gate_epoch(gate_context)
            self.assertEqual(epoch_three["record"]["epoch_number"], 3)
            self.assertEqual(epoch_three["record"]["prior_epoch_sha256"], epoch_two["digest"])
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "rev-parse",
                        f"{epoch_three['record']['validated_code_sha']}^1",
                    ],
                    cwd=repo,
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
                epoch_two["record"]["validated_code_sha"],
            )
            self.assertEqual(cost_history_path.read_bytes(), cost_history)
            with self.assertRaisesRegex(
                agentteam_module.AgentTeamCliError,
                "gate epoch is stale",
            ):
                agentteam_module._gate_register(
                    repo,
                    profile,
                    run_dir,
                    gate_id="P1-LIVE",
                    gate_epoch=1,
                    evidence_run_id="live-evidence-run",
                    expected_integration_head=baseline_head,
                )
            self.assertEqual(receipt_path.read_bytes(), epoch_one_receipt)
            self.assertEqual(epoch_one_approval_path.read_bytes(), epoch_one_approval)

            subprocess.run(
                [
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    refreshed_again["integration_worktree"],
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            missing_worktree_status = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            missing_worktree_payload = json.loads(missing_worktree_status.stdout)
            self.assertEqual(
                missing_worktree_payload["post_backlog_gates"]["state"],
                "failed_closed",
            )
            self.assertIsNone(
                missing_worktree_payload["integration_baseline"]["head_sha"]
            )
            repair_action = missing_worktree_payload["post_backlog_gates"][
                "repair_action"
            ]
            self.assertEqual(
                missing_worktree_payload["next_action"],
                repair_action,
            )
            self.assertIn("gate refresh-baseline", repair_action)


    def test_post_backlog_gate_git_oid_format_and_operator_tty_fail_closed(self):
        self.assertTrue(agentteam_module._valid_git_oid("a" * 40, "sha1"))
        self.assertFalse(agentteam_module._valid_git_oid("a" * 64, "sha1"))
        self.assertTrue(agentteam_module._valid_git_oid("b" * 64, "sha256"))
        self.assertFalse(agentteam_module._valid_git_oid("b" * 40, "sha256"))
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(agentteam_module.AgentTeamCliError):
                agentteam_module._require_operator_approval_context()


    def test_agentteam_cli_pursue_help_lists_budget_and_gate_options(self):
        completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", "pursue", "--help"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--max-rounds", completed.stdout)
        self.assertIn("--stop-on-review-gate", completed.stdout)
        self.assertIn("--allow-review-gate-follow-up", completed.stdout)


    def test_agentteam_cli_pursue_can_continue_when_review_gate_follow_up_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
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
                    "pursue-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--verification-command-json",
                    json.dumps(["python3", "-c", "print('ok')"]),
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
                    "pursue",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "持续优化这个仓库的准确率和延迟。",
                    "--taskpack-id",
                    "pursue-loop",
                    "--max-rounds",
                    "2",
                    "--allow-review-gate-follow-up",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["stop_reason"], "max_rounds_reached")
            self.assertEqual(summary["rounds_completed"], 2)
            self.assertEqual(
                [item["taskpack_id"] for item in summary["runs"]],
                ["pursue-loop", "pursue-loop-r2"],
            )
            self.assertTrue((work_root / "runs" / "pursue-loop-r2").exists())

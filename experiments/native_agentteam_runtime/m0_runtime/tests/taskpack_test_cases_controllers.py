try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ControllersMixin:
    def test_controller_only_relation_context_rejects_legacy_unscoped_fallback(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            run_dir = work_root / "runs" / "promotion"
            legacy_context = (
                run_dir
                / "state"
                / "P2-08.relation-context.v1.json"
            )
            _write_json(
                legacy_context,
                {
                    "epoch_number": 2,
                    "epoch_sha256": "2" * 64,
                    "integration_head": "a" * 40,
                },
            )
            declaration = _phase2_controller_gate_declarations()[0]
            with self.assertRaisesRegex(
                agentteam_module.Phase2GateError,
                "relation context is missing or unsafe",
            ):
                agentteam_module._phase2_gate_relation_context(
                    {
                        "work_root": work_root,
                        "run_dir": run_dir,
                        "epochs_root": (
                            run_dir
                            / "state"
                            / "post_backlog_gates"
                            / "epochs"
                        ),
                        "taskpack": {
                            "execution_mode": "controller_only",
                        },
                    },
                    {
                        "record": {"epoch_number": 2},
                        "digest": "2" * 64,
                    },
                    declaration,
                    {},
                    run_dir,
                    "a" * 40,
                )


    def test_phase2_controller_restarts_dependency_chain_after_epoch_refresh(
        self,
    ):
        declarations = _phase2_controller_gate_declarations()
        context = {
            "declarations": declarations,
            "declarations_by_id": {
                item["gate_id"]: item for item in declarations
            },
            "locks_root": Path(tempfile.gettempdir())
            / f"agentteam-gate-lock-test-{uuid.uuid4().hex}",
        }
        epoch_one = {
            "record": {"epoch_number": 1},
            "digest": "1" * 64,
        }
        epoch_two = {
            "record": {"epoch_number": 2},
            "digest": "2" * 64,
        }
        calls = []

        def run_one(_context, current, declaration, prior):
            calls.append(
                (
                    current["record"]["epoch_number"],
                    declaration["gate_id"],
                    tuple(prior),
                )
            )
            if current is epoch_one:
                return {
                    "gate_id": "P2-08",
                    "state": "epoch_refreshed",
                    "epoch_refreshed": True,
                }
            if declaration["gate_id"] == "P2-08":
                return {
                    "gate_id": "P2-08",
                    "state": "passed",
                }
            return {
                "gate_id": declaration["gate_id"],
                "state": "awaiting_operator_authorization",
            }

        with mock.patch.object(
            agentteam_module,
            "_read_current_gate_epoch",
            side_effect=[epoch_one, epoch_two],
        ), mock.patch.object(
            agentteam_module,
            "_run_one_available_phase2_gate_controller",
            side_effect=run_one,
        ):
            results = (
                agentteam_module
                ._run_available_phase2_gate_controllers(context)
            )
        self.assertEqual(
            calls,
            [
                (1, "P2-08", ()),
                (2, "P2-08", ()),
                (2, "P2-09", ("P2-08",)),
            ],
        )
        self.assertEqual(
            [item["state"] for item in results],
            [
                "passed",
                "awaiting_operator_authorization",
                "pending",
            ],
        )


    def test_controller_only_blueprint_materializes_and_freezes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_relative = "plans/example.blueprint.json"
            source_plan_relative = "plans/example.md"
            review_schema_relative = "schemas/review.schema.json"
            approval_relative = "reviews/approval.json"
            (repo / source_plan_relative).parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            (repo / source_plan_relative).write_text(
                "# Phase 2 promotion\n",
                encoding="utf-8",
            )
            fixture_tests = repo / "tests"
            fixture_tests.mkdir()
            (fixture_tests / "test_gate.py").write_text(
                "import unittest\n\n"
                "class GateFixtureTests(unittest.TestCase):\n"
                "    def test_repository_is_verifiable(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (repo / ".gitignore").write_text(
                "__pycache__/\n*.pyc\n",
                encoding="utf-8",
            )
            _write_json(
                repo / review_schema_relative,
                {
                    "$schema": (
                        "https://json-schema.org/draft/2020-12/schema"
                    ),
                    "type": "object",
                },
            )
            schema_root = (
                repo
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
            )
            schema_root.mkdir(parents=True)
            for name in (
                "phase2_readiness_promotion.schema.json",
                "phase2_live_authorization.schema.json",
                "phase2_calibration.schema.json",
                "phase2_finalization.schema.json",
            ):
                shutil.copy2(
                    REPO_ROOT
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / name,
                    schema_root / name,
                )
            schema_prefix = (
                "experiments/native_agentteam_runtime/schemas/"
            )
            for relative_path in (
                experiment_gates_module
                ._CAPABILITY_TEST_ARTIFACT_PATHS.values()
            ):
                artifact_path = repo / relative_path
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    "# fixed registry fixture\n",
                    encoding="utf-8",
                )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add promotion candidate"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            candidate_commit = _git_head(repo)
            authority_root = repo / "authority"
            authority_root.mkdir()
            protocol_template = authority_root / "protocol.json"
            deterministic_calibration = (
                authority_root / "deterministic-calibration.json"
            )
            deterministic_calibration_request = (
                authority_root / "calibration-request.json"
            )
            work_root = tmp_path / "project-work-root"
            work_authority = work_root / "phase2" / "promotion"
            work_authority.mkdir(parents=True)
            evaluator = work_authority / "evaluator.json"
            _write_json(
                repo / ".agentteam" / "profile.json",
                {
                    "profile_schema_version": "agentteam_profile.v1",
                    "project_key": "fixture",
                    "work_root": str(work_root),
                },
            )
            for path, value in (
                (
                    protocol_template,
                    {
                        "protocol": "fixture",
                        "mode_order": [
                            "agentteam_direct",
                            "single_codex",
                            "agentteam_full",
                        ],
                        "repetition_policy": {"count": 2},
                    },
                ),
                (
                    deterministic_calibration_request,
                    {"request": "fixture"},
                ),
                (
                    deterministic_calibration,
                    {"calibration_status": "passed"},
                ),
                (evaluator, {"evaluator": "fixture"}),
            ):
                _write_json(path, value)
            direct_draft = draft_taskpack_files(
                project_root=repo,
                goal="Run the direct Phase 2 fixture.",
                draft_root=tmp_path / "direct-drafts",
                taskpack_id="phase2-direct-fixture",
                read_scope=["."],
                write_scope=["src/"],
            )
            direct_frozen = freeze_taskpack(
                direct_draft["taskpack_dir"],
                authority_root / "direct-taskpacks",
            )
            direct_taskpack_path = Path(
                direct_frozen["frozen_taskpack_dir"]
            )
            direct_taskpack_digest = direct_frozen["manifest"][
                "digest_sha256"
            ]

            def late_bound_binding(path):
                resolved_path = path.resolve()
                try:
                    relative_path = resolved_path.relative_to(
                        repo.resolve()
                    )
                except ValueError:
                    authority_root_name = "project_work_root"
                    relative_path = resolved_path.relative_to(
                        work_root.resolve()
                    )
                else:
                    authority_root_name = "project_root"
                return {
                    "resolve_at_materialization": {
                        "authority_root": authority_root_name,
                        "relative_path": relative_path.as_posix(),
                    }
                }

            gates = [
                {
                    "gate_id": "P2-08",
                    "depends_on": [],
                    "executor": "deterministic_controller",
                    "evidence_artifact": "acceptance/readiness.json",
                    "evidence_schema": (
                        schema_prefix
                        + "phase2_readiness_promotion.schema.json"
                    ),
                    "required_status_field": (
                        "controller_validation_status"
                    ),
                    "required_status_value": "passed",
                    "controller_entrypoint": (
                        "phase2_readiness_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_readiness_relation_v1"
                    ),
                    "operator_authorization_required": False,
                },
                {
                    "gate_id": "P2-09",
                    "depends_on": ["P2-08"],
                    "executor": "deterministic_controller",
                    "evidence_artifact": "acceptance/calibration.json",
                    "evidence_schema": (
                        schema_prefix + "phase2_calibration.schema.json"
                    ),
                    "required_status_field": (
                        "controller_validation_status"
                    ),
                    "required_status_value": "passed",
                    "controller_entrypoint": (
                        "phase2_live_calibration_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_live_calibration_relation_v1"
                    ),
                    "operator_authorization_required": True,
                    "operator_authorization_schema": (
                        schema_prefix
                        + "phase2_live_authorization.schema.json"
                    ),
                    "operator_authorization_required_decision": (
                        "approved"
                    ),
                },
                {
                    "gate_id": "P2-10",
                    "depends_on": ["P2-09"],
                    "executor": "deterministic_controller",
                    "evidence_artifact": "acceptance/finalization.json",
                    "evidence_schema": (
                        schema_prefix + "phase2_finalization.schema.json"
                    ),
                    "required_status_field": (
                        "controller_validation_status"
                    ),
                    "required_status_value": "passed",
                    "controller_entrypoint": (
                        "phase2_finalization_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_finalization_relation_v1"
                    ),
                    "operator_authorization_required": False,
                },
            ]
            for gate in gates:
                gate["controller_action_input"] = (
                    _phase2_controller_action_input(
                        gate["gate_id"]
                    )
                )
                if gate["gate_id"] == "P2-08":
                    gate["controller_action_input"]["configuration"][
                        "capability_evidence"
                    ] = {
                        "resolve_from_registry": {
                            "registry_version": (
                                experiment_gates_module
                                .READINESS_CAPABILITY_REGISTRY_VERSION
                            )
                        }
                    }
                    gate["controller_action_input"]["configuration"][
                        "authority_artifacts"
                    ] = {
                        "protocol_template": late_bound_binding(
                            protocol_template
                        ),
                        "deterministic_calibration_request": (
                            late_bound_binding(
                                deterministic_calibration_request
                            )
                        ),
                        "deterministic_calibration": late_bound_binding(
                            deterministic_calibration
                        ),
                    }
                elif gate["gate_id"] == "P2-09":
                    gate["controller_action_input"]["configuration"][
                        "authority_artifacts"
                    ] = {
                        "evaluator": late_bound_binding(evaluator),
                    }
                    gate["controller_action_input"]["configuration"][
                        "direct_taskpack"
                    ] = late_bound_binding(direct_taskpack_path)
            blueprint = {
                "schema_version": "agentteam_taskpack_blueprint.v1",
                "blueprint_id": "example-blueprint",
                "source_plan": source_plan_relative,
                "contract": {
                    "candidate_source_commit": candidate_commit,
                },
                "taskpack": {
                    "taskpack_id": "example-blueprint",
                    "goal_kind": "implementation",
                    "goal": "Promote the Phase 2 experiment harness.",
                    "overall_risk": "L3",
                    "execution_mode": "controller_only",
                },
                "approval": {
                    "record_path": approval_relative,
                    "schema_path": review_schema_relative,
                    "required_decision": "approved",
                    "git_object_format_required": True,
                    "runtime_release_binding_required": True,
                    "digest_bindings": [
                        "source_plan",
                        "blueprint",
                        "review_schema",
                        "contract",
                    ],
                },
                "agents": [],
                "verification": {
                    "command": [
                        "python3",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                    ],
                },
                "policy": {
                    "allow_merge": False,
                    "merge_requires_verified_integration": True,
                    "operator_review_required": True,
                },
                "tasks": [],
                "post_backlog_gates": gates,
            }
            _write_json(repo / blueprint_relative, blueprint)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add promotion blueprint"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            release_head = _git_head(repo)
            _write_blueprint_approval(repo, blueprint)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "approve promotion blueprint"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            active_release = {
                "release_id": "fixture-release",
                "source_commit": release_head,
            }
            with mock.patch.object(
                taskpack_module,
                "_active_taskpack_blueprint_release",
                return_value=active_release,
            ):
                result = taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_relative,
                    tmp_path / "drafts",
                )
                original_calibration = (
                    deterministic_calibration.read_bytes()
                )
                _write_json(
                    deterministic_calibration,
                    {"calibration_status": "drifted"},
                )
                with self.assertRaisesRegex(
                    TaskpackValidationError,
                    "context changed before freeze",
                ):
                    freeze_taskpack(
                        result["taskpack_dir"],
                        tmp_path / "frozen",
                    )
                deterministic_calibration.write_bytes(
                    original_calibration
                )
                frozen = freeze_taskpack(
                    result["taskpack_dir"],
                    tmp_path / "frozen",
                )
            _write_json(
                deterministic_calibration,
                {"calibration_status": "changed-after-freeze"},
            )
            evaluator.unlink()
            shutil.rmtree(direct_taskpack_path)

            loaded = load_taskpack(frozen["frozen_taskpack_dir"])
            self.assertEqual(
                loaded["taskpack"]["execution_mode"],
                "controller_only",
            )
            self.assertEqual(loaded["backlog"]["items"], [])
            self.assertEqual(loaded["agent_pool"]["agents"], [])
            self.assertEqual(
                [
                    gate["gate_id"]
                    for gate in loaded["taskpack"][
                        "post_backlog_gates"
                    ]
                ],
                ["P2-08", "P2-09", "P2-10"],
            )
            materialization = loaded["taskpack"]["context"][
                "materialized_authority_bindings"
            ]
            self.assertEqual(len(materialization), 5)
            registry_materialization = loaded["taskpack"]["context"][
                "materialized_registry_bindings"
            ]
            self.assertEqual(len(registry_materialization), 1)
            self.assertEqual(
                registry_materialization[0]["candidate_source_commit"],
                candidate_commit,
            )
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            calibration_binding = loaded["taskpack"][
                "post_backlog_gates"
            ][0]["controller_action_input"]["configuration"][
                "authority_artifacts"
            ]["deterministic_calibration"]
            self.assertFalse(
                Path(calibration_binding["path"]).is_absolute()
            )
            self.assertEqual(
                (
                    frozen_dir / calibration_binding["path"]
                ).read_bytes(),
                original_calibration,
            )
            direct_binding = loaded["taskpack"][
                "post_backlog_gates"
            ][1]["controller_action_input"]["configuration"][
                "direct_taskpack"
            ]
            self.assertFalse(Path(direct_binding["path"]).is_absolute())
            self.assertEqual(
                direct_binding["digest_sha256"],
                direct_taskpack_digest,
            )
            taskpack_module.verify_frozen_taskpack_digest(
                frozen_dir / direct_binding["path"],
                direct_taskpack_digest,
            )
            resolved_action = agentteam_module._phase2_action_input(
                {
                    "frozen_dir": frozen_dir,
                },
                loaded["taskpack"]["post_backlog_gates"][0],
            )
            self.assertEqual(
                resolved_action["configuration"][
                    "authority_artifacts"
                ]["deterministic_calibration"]["path"],
                str(
                    (
                        frozen_dir / calibration_binding["path"]
                    ).resolve()
                ),
            )
            consumed_authority = (
                experiment_gates_module._action_authority_files(
                    resolved_action["configuration"][
                        "authority_artifacts"
                    ],
                    required=(
                        "protocol_template",
                        "deterministic_calibration_request",
                        "deterministic_calibration",
                    ),
                    authority_roots=[frozen_dir],
                )
            )
            self.assertEqual(
                Path(
                    consumed_authority[
                        "deterministic_calibration"
                    ]["path"]
                ).read_bytes(),
                original_calibration,
            )
            taskpack_module._validate_controller_action_authority(
                repo,
                loaded["taskpack"]["post_backlog_gates"],
                taskpack_root=frozen_dir,
            )
            with mock.patch.object(
                agentteam_module,
                "_launcher_runtime_selection",
                return_value={"selection_version": "test"},
            ), mock.patch.object(
                agentteam_module,
                "_prepare_bound_implementation_run",
                return_value=None,
            ), mock.patch.object(
                agentteam_module,
                "_run_runtime_command_with_progress",
                side_effect=AssertionError(
                    "worker runtime must not be started"
                ),
            ), mock.patch.object(
                agentteam_module,
                "_execute_phase2_controller_action",
                return_value={
                    "action_status": (
                        "awaiting_operator_authorization"
                    )
                },
            ):
                completed = agentteam_module._run_frozen_taskpack(
                    Path(frozen["frozen_taskpack_dir"]),
                    tmp_path / "runs",
                )
            summary = json.loads(completed.stdout)
            self.assertFalse(summary["worker_pool_started"])
            self.assertEqual(
                summary["status"],
                "awaiting_post_backlog_gates",
            )
            epoch = json.loads(
                (
                    tmp_path
                    / "runs"
                    / loaded["taskpack"]["taskpack_id"]
                    / "state"
                    / "post_backlog_gates"
                    / "epochs"
                    / "1"
                    / "epoch.v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                epoch["integration_head_sha"],
                release_head,
            )


    def test_controller_only_authority_must_exist_before_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "must stay inside|does not exist",
            ):
                taskpack_module._validate_controller_action_authority(
                    root,
                    _phase2_controller_gate_declarations(),
                )


    def test_late_bound_controller_authority_rejects_unsafe_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")

            def blueprint(binding):
                return {
                    "post_backlog_gates": [
                        {
                            "gate_id": "P2-08",
                            "controller_action_input": {
                                "configuration": {
                                    "authority_artifacts": {
                                        "fixture": binding,
                                    }
                                }
                            },
                        }
                    ]
                }

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "must use resolve_at_materialization",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint(
                        {
                            "path": str(outside),
                            "sha256": hashlib.sha256(
                                outside.read_bytes()
                            ).hexdigest(),
                        }
                    ),
                    project_root=repo,
                )
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "normalized relative path",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint(
                        {
                            "resolve_at_materialization": {
                                "authority_root": "project_root",
                                "relative_path": "../outside.json",
                            }
                        }
                    ),
                    project_root=repo,
                )
            (repo / "linked.json").symlink_to(outside)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "path is unsafe",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint(
                        {
                            "resolve_at_materialization": {
                                "authority_root": "project_root",
                                "relative_path": "linked.json",
                            }
                        }
                    ),
                    project_root=repo,
                )


    def test_controller_only_taskpack_requires_blueprint_and_fixed_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Run deterministic Phase 2 promotion gates.",
                draft_root=tmp_path / "drafts",
                taskpack_id="phase2-controller-only",
                write_scope=["src/"],
            )
            taskpack_dir = Path(draft["taskpack_dir"])
            taskpack = json.loads(
                (taskpack_dir / "taskpack.yaml").read_text(
                    encoding="utf-8"
                )
            )
            taskpack.update(
                {
                    "authoring_mode": "blueprint_materialized",
                    "execution_mode": "controller_only",
                    "context": {
                        "runtime_release_id": "candidate-release",
                        "runtime_release_source_commit": _git_head(repo),
                    },
                    "runtime": {
                        "default_backend": "codex",
                        "codex": {},
                    },
                    "post_backlog_gates": (
                        _phase2_controller_gate_declarations()
                    ),
                }
            )
            _write_json(taskpack_dir / "taskpack.yaml", taskpack)
            _write_json(
                taskpack_dir / "backlog.json",
                {
                    "backlog_id": "BL-phase2-controller-only",
                    "items": [],
                },
            )
            agent_pool = json.loads(
                (taskpack_dir / "agent_pool.json").read_text(
                    encoding="utf-8"
                )
            )
            agent_pool["agents"] = []
            agent_pool["role_runtime_profiles"] = {}
            _write_json(taskpack_dir / "agent_pool.json", agent_pool)

            self.assertEqual(
                validate_taskpack(taskpack_dir)["status"],
                "accepted",
            )

            verification = json.loads(
                (taskpack_dir / "verification.json").read_text(
                    encoding="utf-8"
                )
            )
            verification["command"] = [
                "env",
                "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                "python3",
                "-m",
                "unittest",
                "discover",
            ]
            _write_json(taskpack_dir / "verification.json", verification)
            self.assertEqual(
                validate_taskpack(taskpack_dir)["status"],
                "accepted",
            )
            for invalid_assignment in (
                "HOME=relative",
                "PYTHONPATH=../../outside",
                "PYTHONPATH=/absolute/path",
                f"PYTHONPATH=first{os.pathsep}second",
            ):
                verification["command"][1] = invalid_assignment
                _write_json(
                    taskpack_dir / "verification.json",
                    verification,
                )
                with self.assertRaisesRegex(
                    TaskpackValidationError,
                    "verification command is not allowed: env",
                ):
                    validate_taskpack(taskpack_dir)
            verification["command"][1] = (
                "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime"
            )
            _write_json(taskpack_dir / "verification.json", verification)

            taskpack["authoring_mode"] = "legacy_direct"
            _write_json(taskpack_dir / "taskpack.yaml", taskpack)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "must be blueprint_materialized",
            ):
                validate_taskpack(taskpack_dir)
            taskpack["authoring_mode"] = "blueprint_materialized"
            taskpack["post_backlog_gates"][0][
                "relation_validator"
            ] = "phase2_finalization_relation_v1"
            _write_json(taskpack_dir / "taskpack.yaml", taskpack)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "registry binding mismatch",
            ):
                validate_taskpack(taskpack_dir)


    def test_controller_only_launcher_rejects_handcrafted_frozen_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            work_root = tmp_path / "work"
            frozen_dir = (
                work_root / "frozen" / "phase2-controller-launch"
            )
            frozen_dir.mkdir(parents=True)
            _write_json(
                frozen_dir / "taskpack.yaml",
                {
                    "taskpack_schema_version": "taskpack.v1",
                    "taskpack_id": "phase2-controller-launch",
                    "status": "frozen",
                    "authoring_mode": "blueprint_materialized",
                    "execution_mode": "controller_only",
                    "project_root": str(repo),
                    "goal": "Run deterministic Phase 2 promotion gates.",
                    "context": {
                        "runtime_release_id": "candidate-release",
                        "runtime_release_source_commit": _git_head(repo),
                    },
                    "runtime": {
                        "default_backend": "codex",
                        "codex": {},
                    },
                    "files": {
                        "agent_pool": "agent_pool.json",
                        "backlog": "backlog.json",
                        "verification": "verification.json",
                    },
                    "post_backlog_gates": [
                        {
                            "gate_id": "P2-08",
                            "depends_on": [],
                            "executor": "deterministic_controller",
                            "evidence_artifact": (
                                "acceptance/readiness.json"
                            ),
                            "evidence_schema": (
                                "experiments/native_agentteam_runtime/"
                                "schemas/"
                                "phase2_readiness_promotion.schema.json"
                            ),
                            "required_status_field": (
                                "controller_validation_status"
                            ),
                            "required_status_value": "passed",
                            "controller_entrypoint": (
                                "phase2_readiness_controller_v1"
                            ),
                            "relation_validator": (
                                "phase2_readiness_relation_v1"
                            ),
                            "operator_authorization_required": False,
                        }
                    ],
                },
            )
            _write_json(
                frozen_dir / "agent_pool.json",
                {
                    "scheduler_agent_id": "agent-scheduler",
                    "role_runtime_profiles": {},
                    "agents": [],
                },
            )
            _write_json(
                frozen_dir / "backlog.json",
                {
                    "backlog_id": "BL-phase2-controller-launch",
                    "items": [],
                },
            )
            _write_json(
                frozen_dir / "verification.json",
                {
                    "verification_schema_version": (
                        "taskpack_verification.v1"
                    ),
                    "command": ["python3", "-c", "pass"],
                    "success_criteria": ["command succeeds"],
                },
            )
            with mock.patch.object(
                agentteam_module,
                "_run_runtime_command_with_progress",
                side_effect=AssertionError(
                    "worker runtime must not be started"
                ),
            ):
                with self.assertRaisesRegex(
                    agentteam_module.AgentTeamCliError,
                    "frozen manifest is invalid",
                ):
                    agentteam_module._run_frozen_taskpack(
                        frozen_dir,
                        work_root / "runs",
                    )


    def test_controller_only_report_uses_gate_evidence_without_fake_worker_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / "runs" / "controller-run"
            _write_json(
                work_root / "frozen" / "controller-run" / "taskpack.yaml",
                {
                    "taskpack_id": "controller-run",
                    "execution_mode": "controller_only",
                    "post_backlog_gates": [
                        {
                            "gate_id": "P3-READY",
                            "operator_review_required": True,
                        }
                    ],
                },
            )
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "completed",
                    "backlog": {"items": []},
                    "inflight_attempts": [],
                    "steps": [],
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": (
                            "agentteam/run/controller-run/integration"
                        ),
                        "integration_baseline_head_sha": "a" * 40,
                    },
                },
            )
            _write_jsonl(
                run_dir / "events.jsonl",
                [
                    {
                        "event_id": "EVT-0001",
                        "event_type": "run_completed",
                        "sequence": 1,
                        "payload": {
                            "run_status": "completed",
                            "scheduler_status": "completed",
                            "operator_report": {
                                "report_schema_version": "operator_run_report.v1",
                                "task_count": 0,
                                "blocked_count": 0,
                                "task_reports": [],
                            },
                        },
                    }
                ],
            )
            epoch_dir = (
                run_dir / "state" / "post_backlog_gates" / "epochs" / "1"
            )
            _write_json(
                epoch_dir
                / "controller_results"
                / "P3-READY.controller-result.v1.json",
                {
                    "schema_version": "gate_controller_result.v1",
                    "gate_id": "P3-READY",
                    "controller_entrypoint": "phase3_readiness_controller_v1",
                    "controller_status": "passed",
                    "relation_validator": "phase3_readiness_relation_v1",
                    "evidence_sha256": "b" * 64,
                    "provider_calls": 0,
                    "target_mutations": 0,
                },
            )
            _write_json(
                epoch_dir / "approvals" / "P3-READY.approval.v1.json",
                {"gate_id": "P3-READY", "decision": "approved"},
            )

            report = build_run_completion_report(run_dir, write_files=False)
            summary = report["completion_summary"]

            self.assertEqual(report["task_count"], 0)
            self.assertEqual(report["execution_mode"], "controller_only")
            self.assertEqual(report["controller_report"]["status"], "passed")
            self.assertEqual(report["token_usage"]["usage_status"], "not_applicable")
            self.assertEqual(summary["integration"], "not_applicable")
            self.assertEqual(
                summary["changed_files_note_zh"],
                "未修改源文件；本次 controller-only 运行仅发布运行证据。",
            )
            self.assertEqual(summary["evidence_gaps"], [])
            self.assertEqual(
                summary["follow_up_recommendation"]["action"],
                "review_report",
            )
            self.assertNotIn(
                "No task-level operator report was found",
                json.dumps(summary, ensure_ascii=False),
            )
            self.assertNotIn("agentteam integrate", json.dumps(summary))
            self.assertIn("P3-READY", summary["what_changed"][0])
            self.assertIn("provider_calls=0", summary["measured_results"][0])

            guided = agentteam_module._apply_post_backlog_gate_report_guidance(
                report,
                {
                    "all_passed": True,
                    "state": "passed",
                    "gates": [{"gate_id": "P3-READY", "state": "passed"}],
                    "operator_view": {
                        "gate_epoch": 1,
                        "integration_baseline": {
                            "branch": "agentteam/run/controller-run/integration",
                            "base_sha": "a" * 40,
                            "head_sha": "a" * 40,
                        },
                        "review_commands": {
                            "report": "agentteam report --taskpack controller-run",
                            "paths": "agentteam paths --taskpack controller-run",
                            "integrate": "agentteam integrate --taskpack controller-run",
                        },
                    },
                },
            )
            guided_summary = guided["completion_summary"]
            self.assertEqual(guided_summary["review_gate"]["status"], "passed")
            self.assertNotIn("integrate_command", guided_summary["review_gate"])
            self.assertNotIn("agentteam integrate", json.dumps(guided_summary))


    def test_runtime_diagnostic_obeys_containing_experiment_controller(self):
        from agentteam_runtime.diagnostic_chat import (
            run_runtime_diagnostic_chat,
        )
        from agentteam_runtime.experiment_controller import (
            create_experiment_controller,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = _write_failed_integration_run(
                root / "runs" / "taskpack-5"
            )
            capture_path = root / "diagnostic-started"
            fake_codex = root / "fake_codex.py"
            fake_codex.write_text(
                "from pathlib import Path\n"
                f"Path({str(capture_path)!r}).write_text('started')\n",
                encoding="utf-8",
            )
            controller = create_experiment_controller(
                root,
                protocol_id="diagnostic-boundary",
                max_total_tokens=100,
                max_wall_time_seconds=3600,
                soft_warning_ratio=0.8,
                scored=True,
            )
            controller.interrupt()
            context = build_runtime_diagnostic_context(run_dir)
            self.assertTrue(context["experiment_controller_required"])
            context.pop("experiment_controller_reference")
            context.pop("experiment_authority_root")
            context.pop("experiment_controller_required")

            result = run_runtime_diagnostic_chat(
                context,
                codex_command=["python3", str(fake_codex)],
            )

            self.assertEqual(result["chat_status"], "failed")
            self.assertIn(
                "controller state is interrupted",
                result["error"],
            )
            self.assertFalse(capture_path.exists())


    def test_registered_controller_import_preserves_open_and_detects_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "work" / "runs" / "controller-import"
            session_id = "SMOKE-SESSION-001"
            authority_root = (
                run_dir
                / "state"
                / "controller_invocations"
                / "development_smoke"
                / session_id
            )
            authority_root.mkdir(parents=True)
            claim = {
                "claim_schema_version": "model_invocation_controller_claim.v1",
                "project": "project",
                "run_id": "controller-import",
                "taskpack_id": "controller-import",
                "usage_stage": "development_smoke",
                "runtime_execution_session_id": session_id,
                "lifecycle_owner_token": "SMOKE-OWNER-001",
                "authority_root": str(authority_root.resolve()),
            }
            (authority_root / "controller_claim.json").write_text(
                json.dumps(claim, sort_keys=True),
                encoding="utf-8",
            )
            context = _author_model_invocation_context(
                taskpack_id="controller-import",
                draft_root=run_dir,
                model=None,
                supported=False,
                supplied={
                    "project": "project",
                    "run_id": "controller-import",
                    "usage_stage": "development_smoke",
                },
            )
            context.update(
                {
                    "runtime_execution_session_id": session_id,
                    "lifecycle_owner_token": "SMOKE-OWNER-001",
                    "agent_id": "development-smoke-controller",
                    "role": "development_smoke",
                    "usage_stage": "development_smoke",
                }
            )
            lifecycle = InvocationLifecycle(
                authority_root,
                context,
                invocation_id="INV-development-smoke-controller",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())

            imported_open = import_registered_controller_lifecycles(run_dir)
            open_projection = replay_model_invocation_events(
                run_dir / "events.jsonl"
            )

            self.assertEqual(len(imported_open), 1)
            self.assertEqual(
                open_projection["open_invocation_ids"],
                [lifecycle.invocation_id],
            )
            lifecycle.finalize(
                "completed",
                terminal_writer="development_smoke_controller",
                finished_at="2026-07-23T00:00:01Z",
            )
            self.assertEqual(
                len(import_registered_controller_lifecycles(run_dir)),
                1,
            )
            self.assertEqual(
                import_registered_controller_lifecycles(run_dir),
                [],
            )
            closed_projection = replay_model_invocation_events(
                run_dir / "events.jsonl"
            )
            self.assertEqual(closed_projection["terminal_count"], 1)

            conflicting = json.loads(
                lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            conflicting["terminal_status"] = "failed"
            lifecycle.terminal_path.write_text(
                json.dumps(conflicting, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaises(ModelInvocationIntegrityError):
                import_registered_controller_lifecycles(run_dir)

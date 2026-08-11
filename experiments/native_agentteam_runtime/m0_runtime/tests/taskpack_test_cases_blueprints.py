try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class BlueprintsMixin:
    def test_blueprint_materializes_all_tasks_edges_and_five_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "drafts"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                output_root,
            )

            self.assertEqual(result["task_ids"], ["T-1", "T-2", "T-3"])
            self.assertEqual(result["task_count"], 3)
            self.assertEqual(result["dependency_edge_count"], 2)
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue(result["freeze_eligible"])
            taskpack_dir = Path(result["taskpack_dir"])
            self.assertEqual(
                {path.name for path in taskpack_dir.iterdir()},
                {
                    "taskpack.yaml",
                    "agent_pool.json",
                    "backlog.json",
                    "verification.json",
                    "README.md",
                },
            )
            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            self.assertEqual(backlog["items"], blueprint["tasks"])
            generated_taskpack = json.loads(
                (taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(generated_taskpack["policy"], blueprint["policy"])
            self.assertEqual(
                generated_taskpack["post_backlog_gates"],
                blueprint["post_backlog_gates"],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            self.assertTrue(
                (output_root / "example-blueprint.materialization_manifest.json").is_file()
            )
            self.assertFalse((output_root / "materialization_manifest.json").exists())
            frozen = freeze_taskpack(taskpack_dir, tmp_path / "frozen")
            frozen_taskpack = load_taskpack(frozen["frozen_taskpack_dir"])["taskpack"]
            self.assertEqual(
                frozen_taskpack["context"],
                generated_taskpack["context"],
            )


    def test_taskpack_materialize_cli_blueprint_dry_run_defaults_project_root_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "unused"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "materialize",
                    "--blueprint-file",
                    blueprint_path,
                    "--output-root",
                    str(output_root),
                    "--dry-run",
                    "--json",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["materialize_status"], "dry-run")
            self.assertEqual(summary["source_kind"], "blueprint")
            self.assertEqual(summary["taskpack_id"], "example-blueprint")
            self.assertEqual(summary["task_count"], 3)
            self.assertEqual(summary["dependency_edge_count"], 2)
            self.assertEqual(summary["validation"]["status"], "accepted")
            self.assertEqual(
                summary["blueprint_sha256"],
                summary["manifest"]["blueprint_sha256"],
            )
            self.assertFalse(summary["freeze_eligible"])
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertIsNone(summary["manifest_path"])
            self.assertIsNone(summary["taskpack_dir"])
            self.assertIsNone(summary["frozen_taskpack_dir"])
            self.assertIsNone(summary["paths"]["manifest_path"])
            self.assertIsNone(summary["paths"]["draft_dir"])
            self.assertIsNone(summary["paths"]["frozen_dir"])
            self.assertEqual(len(summary["manifest"]["artifact_digests"]), 5)
            self.assertFalse(output_root.exists())

            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "materialize",
                    "--blueprint-file",
                    blueprint_path,
                    "--output-root",
                    str(output_root),
                    "--dry-run",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertEqual(
                text_completed.stdout.splitlines(),
                [
                    "taskpack_id: example-blueprint",
                    "materialize_status: dry-run",
                    "task_count: 3",
                    "edge_count: 2",
                    "validation: accepted",
                    f"blueprint_sha256: {summary['blueprint_sha256']}",
                    "freeze_eligible: false",
                    "manifest_path: -",
                    "draft_dir: -",
                ],
            )


    def test_taskpack_materialize_cli_rejects_conflicting_retention_and_blueprint_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)

            conflicting = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "materialize",
                    "--blueprint-file",
                    blueprint_path,
                    "--project-root",
                    str(repo),
                    "--output-root",
                    str(output_root),
                    "--dry-run",
                    "--freeze",
                    "--frozen-root",
                    str(frozen_root),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(conflicting.returncode, 1)
            self.assertIn("not allowed with argument --dry-run", conflicting.stderr)

            renamed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "materialize",
                    "--blueprint-file",
                    blueprint_path,
                    "--project-root",
                    str(repo),
                    "--output-root",
                    str(output_root),
                    "--taskpack-id",
                    "renamed-blueprint",
                    "--freeze",
                    "--frozen-root",
                    str(frozen_root),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(renamed.returncode, 1)
            self.assertIn(
                "must equal the approved blueprint taskpack_id",
                renamed.stderr,
            )
            self.assertFalse(output_root.exists())
            self.assertFalse(frozen_root.exists())


    def test_blueprint_materialize_handler_preserves_competing_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)

            def fail_after_competing_publish(
                _taskpack_dir,
                target_root,
                *,
                expected_authoring_mode,
            ):
                self.assertEqual(
                    expected_authoring_mode,
                    "blueprint_materialized",
                )
                competing = Path(target_root) / "example-blueprint"
                competing.mkdir(parents=True)
                (competing / "competitor.marker").write_text(
                    "owned by another publisher",
                    encoding="utf-8",
                )
                raise TaskpackValidationError("publication target exists")

            with mock.patch.object(
                agentteam_module,
                "freeze_taskpack",
                side_effect=fail_after_competing_publish,
            ):
                with self.assertRaisesRegex(
                    TaskpackValidationError,
                    "publication target exists",
                ):
                    _handle_taskpack_materialize(
                        SimpleNamespace(
                            skeleton_taskpack_dir=None,
                            blueprint_file=blueprint_path,
                            project_root=str(repo),
                            output_root=str(output_root),
                            taskpack_id=None,
                            semantic_json=None,
                            semantic_json_file=None,
                            dry_run=False,
                            freeze=True,
                            frozen_root=str(frozen_root),
                            json=True,
                        )
                    )

            self.assertTrue((output_root / "example-blueprint").is_dir())
            self.assertEqual(
                (
                    frozen_root
                    / "example-blueprint"
                    / "competitor.marker"
                ).read_text(encoding="utf-8"),
                "owned by another publisher",
            )


    def test_blueprint_preserves_order_while_edges_remain_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["tasks"] = [
                blueprint["tasks"][2],
                blueprint["tasks"][0],
                blueprint["tasks"][1],
            ]
            _write_json(repo / blueprint_path, blueprint)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )

            self.assertEqual(result["task_ids"], ["T-3", "T-1", "T-2"])
            self.assertEqual(
                result["dependency_edges"],
                [
                    {"task_id": "T-3", "depends_on": "T-2"},
                    {"task_id": "T-2", "depends_on": "T-1"},
                ],
            )


    def test_blueprint_enforces_write_scope_cardinality_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["contract"] = {
                "write_scope_cardinality_policy": {
                    "default_max_entries": 2,
                    "exact_path_exceptions": {"T-2": 3},
                }
            }
            blueprint["tasks"][1]["write_scope"].append("src/task_2_helper.py")
            blueprint["tasks"][1]["write_scope"].append("src/task_2_extra.py")
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "accepted",
                dry_run=True,
            )
            self.assertEqual(result["validation_status"], "accepted")

            blueprint["tasks"][0]["write_scope"].append("src/task_1_helper.py")
            blueprint["tasks"][0]["write_scope"].append("src/task_1_extra.py")
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "T-1 write_scope count 3 exceeds the contract default maximum 2",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "rejected",
                    dry_run=True,
                )


    def test_blueprint_negative_schema_dag_and_path_cases_fail_before_output(self):
        cases = {
            "unknown-dependency": lambda value: value["tasks"][0]["depends_on"].append("missing"),
            "duplicate-id": lambda value: value["tasks"][1].update(task_id="T-1"),
            "cycle": lambda value: value["tasks"][0]["depends_on"].append("T-3"),
            "unknown-field": lambda value: value["tasks"][0].update(unknown=True),
            "absolute-scope": lambda value: value["tasks"][0]["write_scope"].append("/tmp/out"),
            "path-traversal": lambda value: value["tasks"][0]["read_scope"].append("../secret"),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    output_root = tmp_path / "drafts"
                    _init_repo(repo)
                    blueprint_path, blueprint = _blueprint_fixture(repo)
                    mutate(blueprint)
                    _write_json(repo / blueprint_path, blueprint)

                    with self.assertRaises(TaskpackValidationError):
                        taskpack_module.materialize_taskpack_blueprint(
                            repo,
                            blueprint_path,
                            output_root,
                            dry_run=True,
                        )

                    self.assertFalse(output_root.exists())


    def test_blueprint_rejects_symlink_escape_and_conflicting_role_profiles_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "drafts"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            (repo / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
            blueprint["tasks"][0]["write_scope"] = ["escape/generated.py"]
            _write_json(repo / blueprint_path, blueprint)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "resolves outside repository",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    output_root,
                    dry_run=True,
                )
            self.assertFalse(output_root.exists())

            blueprint["tasks"][0]["write_scope"] = ["src/task_1.py"]
            blueprint["post_backlog_gates"][0]["evidence_schema"] = (
                "escape/generated.schema.json"
            )
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "resolves outside repository",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    output_root,
                    dry_run=True,
                )
            self.assertFalse(output_root.exists())

            blueprint["post_backlog_gates"][0]["evidence_schema"] = (
                "src/final.schema.json"
            )
            blueprint["agents"].append(
                {
                    "agent_id": "agent-implementation-worker-2",
                    "role": "implementation_worker",
                    "runtime_profile": {
                        "adapter": "codex",
                        "model": "different-model",
                    },
                }
            )
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "must use one runtime_profile",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    output_root,
                )
            self.assertFalse(output_root.exists())


    def test_blueprint_manifest_exactly_matches_complete_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )

            self.assertEqual(result["task_ids"], ["T-1", "T-2", "T-3"])
            self.assertNotEqual(result["task_ids"], ["T-1"])
            self.assertEqual(len(result["artifact_digests"]), 5)


    def test_blueprint_repeated_dry_materialization_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)

            first = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused-a",
                dry_run=True,
            )
            second = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused-b",
                dry_run=True,
            )

            self.assertEqual(first["artifact_digests"], second["artifact_digests"])
            self.assertEqual(first["dependency_edges"], second["dependency_edges"])
            self.assertFalse((tmp_path / "unused-a").exists())
            self.assertFalse((tmp_path / "unused-b").exists())


    def test_blueprint_addition_preserves_one_task_semantic_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Implement one bounded compatibility task.",
                draft_root=tmp_path / "skeletons",
                taskpack_id="one-task-compatibility",
            )
            semantic_task = taskpack_module.derive_semantic_task_from_skeleton(
                skeleton["taskpack_dir"]
            )

            materialized = taskpack_module.materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                tmp_path / "materialized",
                semantic_task,
            )

            backlog = load_taskpack(materialized["taskpack_dir"])["backlog"]
            self.assertEqual(len(backlog["items"]), 1)
            self.assertEqual(validate_taskpack(materialized["taskpack_dir"])["status"], "accepted")


    def test_blueprint_approval_failures_are_dry_diagnostics_and_block_retention(self):
        scenarios = [
            ("missing", None, None),
            ("pending", "pending", []),
            ("rejected", "rejected", []),
            ("escalated", "approved", ["operator decision required"]),
            ("stale-digest", "approved", []),
        ]
        for label, decision, escalations in scenarios:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    output_root = tmp_path / "drafts"
                    _init_repo(repo)
                    blueprint_path, blueprint = _blueprint_fixture(repo)
                    approval_path = repo / blueprint["approval"]["record_path"]
                    if label == "missing":
                        approval_path.unlink()
                    else:
                        record = _write_blueprint_approval(
                            repo,
                            blueprint,
                            decision=decision,
                            escalations=escalations,
                        )
                        if label == "stale-digest":
                            record["blueprint_sha256"] = "0" * 64
                            _write_json(approval_path, record)

                    dry_result = taskpack_module.materialize_taskpack_blueprint(
                        repo,
                        blueprint_path,
                        tmp_path / "unused",
                        dry_run=True,
                    )
                    self.assertFalse(dry_result["freeze_eligible"])
                    self.assertTrue(dry_result["approval_diagnostics"])

                    with self.assertRaises(TaskpackValidationError):
                        taskpack_module.materialize_taskpack_blueprint(
                            repo,
                            blueprint_path,
                            output_root,
                        )
                    self.assertFalse((output_root / "example-blueprint").exists())
                    self.assertFalse(
                        (
                            output_root
                            / "example-blueprint.materialization_manifest.json"
                        ).exists()
                    )


    def test_tracked_phase1_blueprint_dry_run_has_exact_task_and_edge_counts(self):
        project_root = Path(__file__).resolve().parents[4]
        blueprint_path = (
            "experiments/native_agentteam_runtime/implementation_artifacts/plans/"
            "2026-07-23-phase1-model-invocation-usage.blueprint.json"
        )

        result = taskpack_module.materialize_taskpack_blueprint(
            project_root,
            blueprint_path,
            Path(tempfile.gettempdir()) / "unused-phase1-blueprint-output",
            dry_run=True,
        )

        self.assertEqual(
            result["task_ids"],
            [
                "P1-02A",
                "P1-02B",
                "P1-03",
                "P1-04A",
                "P1-04B",
                "P1-05",
                "P1-06A",
                "P1-06B",
                "P1-06C",
                "P1-06D",
            ],
        )
        self.assertEqual(result["task_count"], 10)
        self.assertEqual(result["dependency_edge_count"], 9)
        self.assertEqual(result["validation_status"], "accepted")
        self.assertFalse(result["freeze_eligible"])
        blueprint = json.loads((project_root / blueprint_path).read_text())
        self.assertEqual(
            blueprint["contract"]["recovered_completed_seed_tasks"],
            {
                "P1-01": (
                    "bc38dfb753de6424935888439965add16197c351"
                )
            },
        )

        tasks_by_id = {
            item["task_id"]: item
            for item in blueprint["tasks"]
        }
        self.assertEqual(tasks_by_id["P1-02A"]["depends_on"], [])
        self.assertEqual(
            [
                item["task_id"]
                for item in blueprint["tasks"]
                if not item["depends_on"]
            ],
            ["P1-02A"],
        )
        self.assertTrue(
            {
                (
                    "experiments/native_agentteam_runtime/m0_runtime/"
                    "agentteam_runtime/token_usage.py"
                ),
                (
                    "experiments/native_agentteam_runtime/m0_runtime/tests/"
                    "test_model_invocation_usage.py"
                ),
                (
                    "experiments/native_agentteam_runtime/schemas/"
                    "event.schema.json"
                ),
                (
                    "experiments/native_agentteam_runtime/schemas/"
                    "model_invocation_started.schema.json"
                ),
                (
                    "experiments/native_agentteam_runtime/schemas/"
                    "model_invocation_usage.schema.json"
                ),
                (
                    "experiments/native_agentteam_runtime/schemas/"
                    "model_invocation_writer_revoked.schema.json"
                ),
                (
                    "experiments/native_agentteam_runtime/schemas/"
                    "model_invocation_live_smoke.schema.json"
                ),
            }.issubset(set(tasks_by_id["P1-02A"]["read_scope"]))
        )

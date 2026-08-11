try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ValidationMixin:
    def test_taskpack_materialize_cli_blueprint_freezes_with_structured_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_root = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
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
                    "--project-root",
                    str(repo),
                    "--output-root",
                    str(output_root),
                    "--freeze",
                    "--frozen-root",
                    str(frozen_root),
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
            self.assertEqual(summary["materialize_status"], "frozen")
            self.assertEqual(summary["task_count"], 3)
            self.assertEqual(summary["dependency_edge_count"], 2)
            self.assertEqual(summary["validation"]["status"], "accepted")
            self.assertEqual(
                summary["manifest_path"],
                str(
                    output_root
                    / "example-blueprint.materialization_manifest.json"
                ),
            )
            self.assertEqual(
                summary["taskpack_dir"],
                str(output_root / "example-blueprint"),
            )
            self.assertEqual(
                summary["frozen_taskpack_dir"],
                str(frozen_root / "example-blueprint"),
            )
            self.assertEqual(
                summary["paths"]["manifest_path"],
                str(
                    output_root
                    / "example-blueprint.materialization_manifest.json"
                ),
            )
            self.assertEqual(
                summary["paths"]["draft_dir"],
                str(output_root / "example-blueprint"),
            )
            self.assertEqual(
                summary["paths"]["frozen_dir"],
                str(frozen_root / "example-blueprint"),
            )
            self.assertTrue(
                (Path(summary["paths"]["frozen_dir"]) / "manifest.json").is_file()
            )


    def test_blueprint_freeze_revalidates_approval_and_cleans_failed_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "drafts",
            )
            _write_blueprint_approval(repo, blueprint, decision="rejected")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(materialized["taskpack_dir"], tmp_path / "frozen")

            self.assertFalse((tmp_path / "frozen" / "example-blueprint").exists())


    def test_blueprint_freeze_rejects_authoring_mode_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)
            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "drafts",
            )
            taskpack_path = (
                Path(materialized["taskpack_dir"]) / "taskpack.yaml"
            )
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("authoring_mode")
            taskpack.pop("context")
            _write_json(taskpack_path, taskpack)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "taskpack authoring provenance changed before freeze",
            ):
                freeze_taskpack(
                    materialized["taskpack_dir"],
                    tmp_path / "frozen",
                    expected_authoring_mode="blueprint_materialized",
                )

            self.assertFalse(
                (tmp_path / "frozen" / "example-blueprint").exists()
            )


    def test_blueprint_freeze_rejects_double_provenance_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)
            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "drafts",
            )
            taskpack_path = (
                Path(materialized["taskpack_dir"]) / "taskpack.yaml"
            )
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("authoring_mode")
            taskpack.pop("context")
            _write_json(taskpack_path, taskpack)
            Path(materialized["manifest_path"]).unlink()

            with self.assertRaisesRegex(
                TaskpackValidationError,
                (
                    "taskpack authoring provenance changed before freeze: "
                    "expected blueprint_materialized, found legacy_direct"
                ),
            ):
                freeze_taskpack(
                    materialized["taskpack_dir"],
                    tmp_path / "frozen",
                    expected_authoring_mode="blueprint_materialized",
                )

            self.assertFalse(
                (tmp_path / "frozen" / "example-blueprint").exists()
            )


    def test_blueprint_freeze_publishes_regenerated_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "drafts",
            )
            original_prepare = (
                taskpack_module._blueprint_taskpack_freeze_source
            )

            def mutate_draft_after_verification(*args, **kwargs):
                source_dir = original_prepare(*args, **kwargs)
                backlog_path = (
                    Path(materialized["taskpack_dir"]) / "backlog.json"
                )
                backlog = json.loads(
                    backlog_path.read_text(encoding="utf-8")
                )
                backlog["items"][0]["objective"] = "Unapproved objective."
                _write_json(backlog_path, backlog)
                return source_dir

            with mock.patch.object(
                taskpack_module,
                "_blueprint_taskpack_freeze_source",
                side_effect=mutate_draft_after_verification,
            ):
                frozen = freeze_taskpack(
                    materialized["taskpack_dir"],
                    tmp_path / "frozen",
                )

            frozen_backlog = json.loads(
                (
                    Path(frozen["frozen_taskpack_dir"]) / "backlog.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                frozen_backlog["items"][0]["objective"],
                blueprint["tasks"][0]["objective"],
            )


    def test_freeze_taskpack_copy_failure_leaves_no_partial_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded change.",
                draft_root=tmp_path / "drafts",
                taskpack_id="transactional-freeze",
                write_scope=["src/"],
            )
            frozen_root = tmp_path / "frozen"
            original_copy = shutil.copy2
            copy_count = 0

            def fail_second_copy(*args, **kwargs):
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("injected copy failure")
                return original_copy(*args, **kwargs)

            with mock.patch.object(
                taskpack_module.shutil,
                "copy2",
                side_effect=fail_second_copy,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected copy failure",
                ):
                    freeze_taskpack(
                        draft["taskpack_dir"],
                        frozen_root,
                    )

            self.assertFalse(
                (frozen_root / "transactional-freeze").exists()
            )
            self.assertEqual(
                list(frozen_root.glob(".transactional-freeze.freezing-*")),
                [],
            )


    def test_blueprint_freeze_copy_failure_leaves_no_partial_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, _blueprint = _blueprint_fixture(repo)
            materialized = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "drafts",
            )
            frozen_root = tmp_path / "frozen"
            original_copy = shutil.copy2
            copy_count = 0

            def fail_second_copy(*args, **kwargs):
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("injected blueprint copy failure")
                return original_copy(*args, **kwargs)

            with mock.patch.object(
                taskpack_module.shutil,
                "copy2",
                side_effect=fail_second_copy,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected blueprint copy failure",
                ):
                    freeze_taskpack(
                        materialized["taskpack_dir"],
                        frozen_root,
                        expected_authoring_mode="blueprint_materialized",
                    )

            self.assertFalse(
                (frozen_root / "example-blueprint").exists()
            )
            self.assertEqual(
                list(frozen_root.glob(".example-blueprint.freezing-*")),
                [],
            )


    def test_freeze_taskpack_publish_race_does_not_replace_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded change.",
                draft_root=tmp_path / "drafts",
                taskpack_id="publish-race",
                write_scope=["src/"],
            )
            frozen_root = tmp_path / "frozen"
            original_rename = taskpack_module._rename_noreplace

            def create_competing_target(source, target):
                Path(target).mkdir()
                return original_rename(source, target)

            with mock.patch.object(
                taskpack_module,
                "_rename_noreplace",
                side_effect=create_competing_target,
            ):
                with self.assertRaisesRegex(
                    TaskpackValidationError,
                    "frozen taskpack publication failed",
                ):
                    freeze_taskpack(
                        draft["taskpack_dir"],
                        frozen_root,
                    )

            competing_target = frozen_root / "publish-race"
            self.assertTrue(competing_target.is_dir())
            self.assertEqual(list(competing_target.iterdir()), [])
            self.assertEqual(
                list(frozen_root.glob(".publish-race.freezing-*")),
                [],
            )


    def test_validate_taskpack_rejects_optimization_taskpack_without_code_facing_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository.",
                draft_root=drafts,
                taskpack_id="doc-only-optimization",
                write_scope=["README.md"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["work_type"] = "audit"
            backlog["items"][0]["write_scope"] = ["README.md"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(taskpack_dir)

            self.assertIn("optimization taskpack requires", str(raised.exception))


    def test_validate_taskpack_rejects_broad_framework_goal_with_documentation_only_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Strengthen the AgentTeam framework long-run reliability.",
                draft_root=drafts,
                taskpack_id="doc-only-framework",
                write_scope=["docs/reliability.md"],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("broad framework taskpack requires", str(raised.exception))


    def test_validate_taskpack_rejects_broad_framework_goal_missing_quality_deliverables(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Strengthen the AgentTeam framework long-run reliability.",
                draft_root=drafts,
                taskpack_id="missing-framework-deliverables",
                write_scope=["src/runtime.py"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            _implementation_item(backlog)["required_deliverables"] = [
                "goal_alignment_summary",
                "verification_summary",
            ]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("broad framework required_deliverables missing", str(raised.exception))


    def test_validate_taskpack_rejects_goal_kind_downgrade_for_optimization_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="阅读这个比赛代码仓库并检查能否优化现有工作。",
                draft_root=drafts,
                taskpack_id="misclassified-optimization",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["goal_kind"] = "audit"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("goal_kind must match original_goal classification: optimization", str(raised.exception))


    def test_validate_taskpack_rejects_optimization_task_that_loses_optimization_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="对比赛代码仓库进行阅读，并检查能否优化现有工作。",
                draft_root=drafts,
                taskpack_id="lost-optimization-intent",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            task["objective"] = "Audit repository completeness and fix concrete in-repo gaps."
            task["goal_alignment"] = "Check whether the repository is ready to submit."
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("optimization task must preserve optimization intent", str(raised.exception))


    def test_validate_taskpack_rejects_optimization_without_decomposition_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository latency.",
                draft_root=drafts,
                taskpack_id="generic-optimization",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            task["objective"] = "Optimize the code."
            task["goal_alignment"] = "This task improves latency."
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn(
                "optimization task must include baseline/profile/candidate/metric decomposition intent",
                str(raised.exception),
            )


    def test_validate_taskpack_rejects_generic_long_running_followup_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the original implementation.\n\n"
                    "Previous taskpack context:\n"
                    "- source_taskpack_id: first-pass\n"
                    "- source_report_path: /tmp/work/runs/first-pass/reports/final_report.md\n\n"
                    "Instructions for the new taskpack:\n"
                    "- Use the previous findings, verification results, blockers, and next steps as context."
                ),
                draft_root=drafts,
                taskpack_id="generic-followup",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            task["objective"] = "Make a small safe cleanup."
            task["goal_alignment"] = "This is a low-risk follow-up task."
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn(
                "long-running follow-up task must define a measurable next-step implementation objective",
                str(raised.exception),
            )


    def test_validate_taskpack_rejects_followup_objective_without_concrete_previous_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the original implementation.\n\n"
                    "Previous taskpack context:\n"
                    "- source_taskpack_id: first-pass\n"
                    "- source_report_path: /tmp/work/runs/first-pass/reports/final_report.md\n\n"
                    "Instructions for the new taskpack:\n"
                    "- Use the previous findings, verification results, blockers, and next steps as context."
                ),
                draft_root=drafts,
                taskpack_id="vague-evidence-followup",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            task["objective"] = (
                "Implement the next step using previous report evidence."
            )
            task["goal_alignment"] = (
                "This follows the selected next_goal but does not name the source report, "
                "verification result, blocker, or goal memory that justified the choice."
            )
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn(
                "long-running follow-up task must tie objective to concrete previous evidence",
                str(raised.exception),
            )


    def test_validate_taskpack_accepts_measurable_followup_objective_with_concrete_previous_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the original implementation.\n\n"
                    "Previous taskpack context:\n"
                    "- source_taskpack_id: first-pass\n"
                    "- source_report_path: /tmp/work/runs/first-pass/reports/final_report.md\n\n"
                    "Long-goal memory:\n"
                    "- goal_memory_path: /tmp/work/state/goal_memory.json\n"
                    "- completed_rounds: 1\n\n"
                    "Instructions for the new taskpack:\n"
                    "- Use the previous findings, verification results, blockers, and next steps as context."
                ),
                draft_root=drafts,
                taskpack_id="evidence-tied-followup",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["objective"] = (
                "Implement the measurable next step from source_report_path "
                "/tmp/work/runs/first-pass/reports/final_report.md: fix the verification "
                "results blocker and verify it with a focused regression test."
            )
            backlog["items"][0]["goal_alignment"] = (
                "The selected next_goal is justified by goal_memory_path "
                "/tmp/work/state/goal_memory.json and the previous report blocker."
            )
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_taskpack_new_uses_profile_and_can_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            profile = build_project_profile(
                repo,
                project_key="fixture",
                work_root=work_root,
                author_runtime="codex",
                default_runtime="auto",
                codex_model="medium",
                verification_profile={
                    "verification_profile_schema_version": "agentteam_verification_profile.v1",
                    "correctness": {"command": ["python3", "tools/check.py"]},
                    "performance": {
                        "command": ["python3", "tools/bench.py", "--json"],
                        "metrics": ["accuracy", "latency_ms"],
                    },
                },
            )
            write_project_profile(repo, profile, force=True)

            result = _handle_taskpack_new(
                SimpleNamespace(
                    project_root=str(repo),
                    work_root=None,
                    goal="Optimize fixture code.",
                    taskpack_id="quick-optimization",
                    read_scope=["."],
                    write_scope=["src/"],
                    verification_command_json=None,
                    allow_merge=False,
                    codex_timeout_seconds=123,
                    freeze=True,
                    json=True,
                )
            )

            self.assertEqual(result["new_status"], "frozen")
            self.assertEqual(result["taskpack_id"], "quick-optimization")
            frozen_dir = Path(result["frozen"]["frozen_taskpack_dir"])
            self.assertTrue((frozen_dir / "taskpack.yaml").exists())
            validation = validate_taskpack(frozen_dir)
            self.assertEqual(validation["status"], "accepted")
            loaded = load_taskpack(frozen_dir)
            self.assertEqual(loaded["verification"]["command"], ["python3", "tools/check.py"])
            self.assertEqual(loaded["taskpack"]["runtime"]["codex"]["model"], "medium")
            self.assertEqual(
                loaded["agent_pool"]["role_runtime_profiles"]["implementation_worker"]["model"],
                "medium",
            )
            self.assertEqual(
                loaded["verification"]["performance"]["command"],
                ["python3", "tools/bench.py", "--json"],
            )
            self.assertEqual(loaded["verification"]["performance"]["metrics"], ["accuracy", "latency_ms"])


    def test_taskpack_materialize_handler_freezes_semantic_completion_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="handler-skeleton",
                context_refs={"source_report_path": "/tmp/work/report.md"},
            )
            semantic_file = tmp_path / "semantic.json"
            semantic_file.write_text(
                json.dumps(
                    {
                        "objective": "Implement the bounded report-backed improvement.",
                        "goal_alignment": "Uses the supplied source report as evidence for the bounded change.",
                        "read_scope": ["src/"],
                        "write_scope": ["src/feature.py"],
                        "required_deliverables": ["verification_summary", "recommended_next_implementation_tasks"],
                    }
                ),
                encoding="utf-8",
            )

            result = _handle_taskpack_materialize(
                SimpleNamespace(
                    skeleton_taskpack_dir=skeleton["taskpack_dir"],
                    output_root=str(materialized_root),
                    taskpack_id="handler-executable",
                    semantic_json=None,
                    semantic_json_file=str(semantic_file),
                    freeze=True,
                    frozen_root=str(frozen_root),
                    json=True,
                )
            )

            self.assertEqual(result["materialize_status"], "frozen")
            self.assertEqual(result["taskpack_id"], "handler-executable")
            self.assertEqual(result["validation"]["status"], "accepted")
            self.assertTrue((Path(result["frozen"]["frozen_taskpack_dir"]) / "taskpack.yaml").exists())


    def test_validate_taskpack_rejects_missing_goal_alignment_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository.",
                draft_root=drafts,
                taskpack_id="missing-goal-alignment",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            del backlog["items"][0]["goal_alignment"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(taskpack_dir)

            self.assertIn("goal_alignment", str(raised.exception))


    def test_validate_taskpack_rejects_missing_required_deliverables_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository.",
                draft_root=drafts,
                taskpack_id="missing-required-deliverables",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["required_deliverables"] = []
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(taskpack_dir)

            self.assertIn("required_deliverables", str(raised.exception))


    def test_validate_taskpack_accepts_legacy_frozen_without_semantic_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Run legacy frozen taskpack.",
                draft_root=drafts,
                taskpack_id="legacy-frozen",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["status"] = "frozen"
            del taskpack["semantic_contract_version"]
            del taskpack["original_goal"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            del backlog["items"][0]["goal_alignment"]
            del backlog["items"][0]["required_deliverables"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")


    def test_load_taskpack_rejects_companion_paths_outside_taskpack_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            cases = [
                ("../agent_pool.json", lambda taskpack_dir: taskpack_dir.parent / "agent_pool.json"),
                (str(tmp_path / "outside.json"), lambda taskpack_dir: tmp_path / "outside.json"),
            ]
            for index, (unsafe_path, target_path_for) in enumerate(cases):
                with self.subTest(unsafe_path=unsafe_path):
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject unsafe companion file paths.",
                        draft_root=drafts,
                        taskpack_id=f"loader-{index}",
                    )
                    taskpack_dir = Path(result["taskpack_dir"])
                    target_path = target_path_for(taskpack_dir)
                    target_path.write_text("{}", encoding="utf-8")
                    taskpack_path = taskpack_dir / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["files"]["agent_pool"] = unsafe_path
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError):
                        load_taskpack(taskpack_dir)


    def test_load_taskpack_rejects_non_object_taskpack_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed taskpack document.",
                draft_root=drafts,
                taskpack_id="malformed-taskpack-yaml",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                load_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack", str(raised.exception))


    def test_validate_taskpack_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject invalid JSON.",
                draft_root=drafts,
                taskpack_id="invalid-json",
                write_scope=["src/"],
            )
            verification_path = Path(result["taskpack_dir"]) / "verification.json"
            verification_path.write_text("{", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("verification.json" in message or "invalid json" in message)


    def test_validate_taskpack_rejects_unbounded_write_scope_matrix(self):
        cases = (
            ("repository-root", ".", "write_scope must not include repository root"),
            ("normalized-repository-root", "./.", "write_scope"),
            ("parent-relative", "../outside", "write_scope"),
            ("root-glob-single", "./*", "write_scope"),
            ("root-glob-recursive", "**/*", "write_scope"),
            ("root-glob-directory", "./**", "write_scope"),
            ("root-glob-directory-recursive", "./**/*", "write_scope"),
            ("root-prefix-extension", "*.py", "write_scope"),
            ("root-prefix-directory", "*/*.py", "write_scope"),
            ("root-prefix-recursive", "*/**/*", "write_scope"),
        )
        for case_id, write_scope, expected_error in cases:
            with self.subTest(case_id=case_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject an unbounded write scope.",
                        draft_root=tmp_path / "drafts",
                        taskpack_id=f"unbounded-write-scope-{case_id}",
                        write_scope=[write_scope],
                    )

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn(expected_error, str(raised.exception))


    def test_validate_taskpack_accepts_scoped_glob_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Accept scoped glob write scope.",
                draft_root=drafts,
                taskpack_id="scoped-glob-write-scope",
                write_scope=["src/**/*.py"],
            )

            validation = validate_taskpack(result["taskpack_dir"])

            self.assertEqual(validation["status"], "accepted")


    def test_validate_taskpack_rejects_invalid_top_level_fields_matrix(self):
        cases = (
            ("missing-taskpack-id", "taskpack_id", "delete", None, "taskpack_id"),
            ("unsafe-taskpack-id", "taskpack_id", "set", "../escaped", "taskpack_id"),
            ("missing-project-root", "project_root", "delete", None, "project_root"),
            ("file-project-root", "project_root", "repo-file", None, "project_root"),
            ("non-string-goal", "goal", "set", ["not", "a", "string"], "goal"),
        )
        for case_id, field, operation, value, expected_error in cases:
            with self.subTest(case_id=case_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject an invalid top-level taskpack field.",
                        draft_root=tmp_path / "drafts",
                        taskpack_id=f"invalid-top-level-{case_id}",
                        write_scope=["src/"],
                    )
                    taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    if operation == "delete":
                        del taskpack[field]
                    elif operation == "repo-file":
                        taskpack[field] = str(repo / "README.md")
                    else:
                        taskpack[field] = value
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn(expected_error, str(raised.exception))


    def test_freeze_taskpack_rejects_unsafe_taskpack_id_without_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            escaped = tmp_path / "escaped"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unsafe freeze target.",
                draft_root=drafts,
                taskpack_id="unsafe-freeze-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["taskpack_id"] = "../escaped"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                freeze_taskpack(result["taskpack_dir"], frozen_root)

            self.assertIn("taskpack_id", str(raised.exception))
            self.assertFalse(escaped.exists())


    def test_validate_taskpack_rejects_missing_verification_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing verification file.",
                draft_root=drafts,
                taskpack_id="missing-verification",
                write_scope=["src/"],
            )
            (Path(result["taskpack_dir"]) / "verification.json").unlink()

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("verification", str(raised.exception))


    def test_validate_taskpack_rejects_invalid_backlog_field_types_matrix(self):
        cases = (
            ("items-not-list", "items", {"task_id": "TASK"}, "backlog.items"),
            ("task-id-not-string", "task_id", ["TASK"], "task_id"),
            ("depends-on-not-list", "depends_on", None, "depends_on"),
            ("dependency-not-string", "depends_on", [123], "depends_on"),
            (
                "write-scope-not-list",
                "write_scope",
                None,
                "write_scope must be a non-empty list",
            ),
            ("write-scope-entry-not-string", "write_scope", [123], "write_scope"),
        )
        for case_id, field, value, expected_error in cases:
            with self.subTest(case_id=case_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject an invalid backlog field type.",
                        draft_root=tmp_path / "drafts",
                        taskpack_id=f"invalid-backlog-field-{case_id}",
                        write_scope=["src/"],
                    )
                    backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
                    backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
                    if field == "items":
                        backlog[field] = value
                    else:
                        backlog["items"][0][field] = value
                    backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn(expected_error, str(raised.exception))


    def test_validate_taskpack_rejects_malformed_task_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task runtime fields.",
                draft_root=drafts,
                taskpack_id="malformed-task-runtime-fields",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            item = backlog["items"][0]
            del item["objective"]
            item["required_role"] = ""
            item["read_scope"] = "src/"
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("objective", message)
            self.assertIn("required_role", message)
            self.assertIn("read_scope", message)


    def test_validate_taskpack_rejects_unknown_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unknown dependency.",
                draft_root=drafts,
                taskpack_id="unknown-dependency",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["task_id"] = "TASK-A"
            backlog["items"][0]["depends_on"] = ["TASK-MISSING"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("depends_on" in message or "unknown" in message)


    def test_validate_taskpack_rejects_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject dependency cycle.",
                draft_root=drafts,
                taskpack_id="dependency-cycle",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task_a = dict(backlog["items"][0])
            task_b = dict(backlog["items"][0])
            task_a["task_id"] = "TASK-A"
            task_a["depends_on"] = ["TASK-B"]
            task_b["task_id"] = "TASK-B"
            task_b["depends_on"] = ["TASK-A"]
            backlog["items"] = [task_a, task_b]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("cycle" in message or "depends_on" in message)


    def test_validate_and_freeze_taskpack_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Freeze a safe taskpack.",
                draft_root=drafts,
                taskpack_id="safe-taskpack",
                write_scope=["src/"],
            )

            validation = validate_taskpack(result["taskpack_dir"])
            self.assertEqual(validation["status"], "accepted")

            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["taskpack_id"], "safe-taskpack")
            self.assertEqual(manifest["status"], "frozen")
            self.assertEqual(len(manifest["digest_sha256"]), 64)
            self.assertTrue((frozen_dir / "taskpack.yaml").exists())


    def test_validate_taskpack_rejects_non_object_agent_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed agent pool.",
                draft_root=drafts,
                taskpack_id="malformed-agent-pool",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("agent_pool", str(raised.exception))


    def test_validate_taskpack_rejects_missing_agent_for_required_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing required role agent.",
                draft_root=drafts,
                taskpack_id="missing-required-role-agent",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["agents"][0]["role"] = "different-role"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("required_role", str(raised.exception))


    def test_validate_taskpack_rejects_non_object_role_runtime_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed role runtime profiles.",
                draft_root=drafts,
                taskpack_id="malformed-role-runtime-profiles",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"] = []
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("role_runtime_profiles", str(raised.exception))


    def test_validate_taskpack_rejects_malformed_role_runtime_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed role runtime profile.",
                draft_root=drafts,
                taskpack_id="malformed-role-runtime-profile",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"] = {"adapter": "unknown"}
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("role_runtime_profiles", str(raised.exception))

            agent_pool["role_runtime_profiles"]["implementation_worker"] = {
                "adapter": "codex",
                "reasoning_profile": 3,
            }
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "reasoning_profile must be a non-empty string",
            ):
                validate_taskpack(result["taskpack_dir"])


    def test_validate_taskpack_rejects_taskpack_runtime_profile_launch_commands(self):
        cases = [
            (
                "role-shell-profile",
                lambda agent_pool: agent_pool["role_runtime_profiles"].__setitem__(
                    "implementation_worker",
                    {"adapter": "shell", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "role-codex-command-profile",
                lambda agent_pool: agent_pool["role_runtime_profiles"].__setitem__(
                    "implementation_worker",
                    {"adapter": "codex", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "agent-shell-profile",
                lambda agent_pool: agent_pool["agents"][0].__setitem__(
                    "runtime_profile",
                    {"adapter": "shell", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "agent-codex-command-profile",
                lambda agent_pool: agent_pool["agents"][0].__setitem__(
                    "runtime_profile",
                    {"adapter": "codex", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
        ]
        for taskpack_id, mutate_agent_pool in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject launch command injection.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
                    agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
                    mutate_agent_pool(agent_pool)
                    agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    message = str(raised.exception)
                    self.assertTrue("adapter" in message or "command" in message)


    def test_validate_taskpack_rejects_malformed_optional_role_maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed optional role maps.",
                draft_root=drafts,
                taskpack_id="malformed-optional-role-maps",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_prompt_contracts"] = []
            agent_pool["role_context_packages"] = []
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("role_prompt_contracts", message)
            self.assertIn("role_context_packages", message)


    def test_freeze_taskpack_rejects_extra_draft_file_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject extra draft files.",
                draft_root=drafts,
                taskpack_id="extra-draft-file",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            (taskpack_dir / "extra.txt").write_text("not inventoried\n", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)

            self.assertFalse((frozen_root / "extra-draft-file").exists())


    def test_freeze_taskpack_rejects_symlink_in_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject symlink artifacts.",
                draft_root=drafts,
                taskpack_id="symlink-draft-file",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            symlink_path = taskpack_dir / "link.json"
            try:
                symlink_path.symlink_to(taskpack_dir / "backlog.json")
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unsupported: {exc}")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)


    def test_freeze_taskpack_rejects_inventoried_symlink_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            outside = tmp_path / "outside-readme.md"
            _init_repo(repo)
            outside.write_text("outside\n", encoding="utf-8")
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject inventoried symlink artifacts.",
                draft_root=drafts,
                taskpack_id="inventoried-symlink",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            readme_path = taskpack_dir / "README.md"
            readme_path.unlink()
            try:
                readme_path.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unsupported: {exc}")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)

            self.assertFalse((frozen_root / "inventoried-symlink").exists())


    def test_freeze_taskpack_honors_companion_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Freeze mapped companion artifacts.",
                draft_root=drafts,
                taskpack_id="mapped-companion",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            nested_dir = taskpack_dir / "nested"
            nested_dir.mkdir()
            (taskpack_dir / "backlog.json").replace(nested_dir / "backlog.json")
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["files"]["backlog"] = "nested/backlog.json"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            validation = validate_taskpack(taskpack_dir)
            self.assertEqual(validation["status"], "accepted")

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((frozen_dir / "nested" / "backlog.json").exists())
            self.assertFalse((frozen_dir / "backlog.json").exists())
            self.assertEqual(len(manifest["digest_sha256"]), 64)


    def test_validate_taskpack_rejects_l1_worker_that_uses_repo_map_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded repository feature.",
                draft_root=drafts,
                taskpack_id="l1-with-repo-map",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            implementation_item = _implementation_item(backlog)
            implementation_item["risk_target"] = "L1"
            _write_json(backlog_path, backlog)

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("L0/L1 tasks must not require repo_map_handoff", str(raised.exception))


    def test_validate_taskpack_rejects_l2_worker_without_repo_map_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded local code change.",
                draft_root=drafts,
                taskpack_id="l2-missing-repo-map",
                write_scope=["src/"],
                risk_target="L1",
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            item = _implementation_item(backlog)
            item["risk_target"] = "L2"
            _write_json(backlog_path, backlog)

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("L2 tasks must consume repo_map_handoff", str(raised.exception))


    def test_validate_taskpack_rejects_l3_worker_without_semantic_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded local code change.",
                draft_root=drafts,
                taskpack_id="l3-direct-implementation",
                write_scope=["src/"],
                risk_target="L1",
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            item = _implementation_item(backlog)
            item["risk_target"] = "L3"
            _write_json(backlog_path, backlog)

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("L3 tasks require semantic_authoring_required", str(raised.exception))


    def test_validate_taskpack_rejects_invalid_runtime_metadata(self):
        cases = [
            ("non-object-validate-runtime", []),
            ("missing-validate-runtime-backend", {}),
            ("unknown-validate-runtime-backend", {"default_backend": "unknown"}),
            ("shell-validate-runtime-backend", {"default_backend": "shell"}),
        ]
        for taskpack_id, runtime in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid runtime metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["runtime"] = runtime
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("runtime", str(raised.exception))
                    with self.assertRaises(TaskpackValidationError):
                        freeze_taskpack(result["taskpack_dir"], frozen_root)
                    self.assertFalse((frozen_root / taskpack_id).exists())

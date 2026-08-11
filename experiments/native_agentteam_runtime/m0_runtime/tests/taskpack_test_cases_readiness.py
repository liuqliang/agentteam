try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ReadinessMixin:
    def test_phase2_existing_promotion_release_is_revalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate_root = root / "gates"
            gate_root.mkdir()
            source_commit = "1" * 40
            recorded_release = {
                "release_id": "phase2-readiness-fixture",
                "release_root": "/recorded/release",
                "runtime_root": "/recorded/runtime",
                "release_manifest_sha256": "2" * 64,
                "source_commit": source_commit,
                "git_object_format": "sha1",
            }
            installed_release = {
                **recorded_release,
                "runtime_root": "/installed/runtime",
            }
            (
                gate_root / "phase2-promotion-release.v1.json"
            ).write_text(
                json.dumps(
                    {
                        "schema_version": (
                            "phase2_promotion_release.v1"
                        ),
                        "release_id": "phase2-readiness-fixture",
                        "source_commit": source_commit,
                        "runtime_release": recorded_release,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                agentteam_module,
                "selected_release_identity",
                return_value=installed_release,
            ) as selected:
                with self.assertRaisesRegex(
                    agentteam_module.Phase2GateError,
                    "record conflicts",
                ):
                    agentteam_module._install_phase2_promotion_release(
                        {
                            "gate_root": gate_root,
                            "work_root": root / "work",
                        },
                        source_commit,
                    )
            selected.assert_called_once_with(
                root / "work",
                "phase2-readiness-fixture",
                expected={"source_commit": source_commit},
            )


    def test_phase2_authorization_rejects_protocol_parameter_drift(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            declaration = _phase2_controller_gate_declarations()[1]
            context = {
                "project_root": root,
                "run_dir": root / "run",
                "declarations_by_id": {
                    "P2-08": _phase2_controller_gate_declarations()[0],
                    "P2-09": declaration,
                },
            }
            current = {
                "record": {"epoch_number": 2},
                "digest": "2" * 64,
            }
            contract = {
                "protocol_sha256": "a" * 64,
                "model": "gpt-5.6",
                "reasoning_profile": "high",
                "max_total_tokens": 1000,
                "max_wall_time_seconds": 600,
            }
            with mock.patch.object(
                agentteam_module,
                "_require_post_backlog_gate_context",
                return_value=context,
            ), mock.patch.object(
                agentteam_module,
                "_require_operator_approval_context",
            ), mock.patch.object(
                agentteam_module,
                "_gate_mutation_locks",
                return_value=nullcontext(),
            ), mock.patch.object(
                agentteam_module,
                "_require_current_gate_epoch",
                return_value=current,
            ), mock.patch.object(
                agentteam_module,
                "_evaluate_post_backlog_gates",
                return_value={
                    "gates": [
                        {
                            "gate_id": "P2-08",
                            "state": "passed",
                            "evidence_sha256": "b" * 64,
                        }
                    ]
                },
            ), mock.patch.object(
                agentteam_module,
                "_phase2_live_authorization_contract",
                return_value=contract,
            ), mock.patch.object(
                agentteam_module,
                "publish_live_authorization",
            ) as publish:
                with self.assertRaisesRegex(
                    agentteam_module.AgentTeamCliError,
                    "differ from the generated",
                ):
                    agentteam_module._gate_authorize(
                        root,
                        {},
                        context["run_dir"],
                        gate_id="P2-09",
                        gate_epoch=2,
                        protocol_sha256=contract[
                            "protocol_sha256"
                        ],
                        model="wrong-model",
                        reasoning_profile=contract[
                            "reasoning_profile"
                        ],
                        max_total_tokens=contract[
                            "max_total_tokens"
                        ],
                        max_wall_time_seconds=contract[
                            "max_wall_time_seconds"
                        ],
                    )
            publish.assert_not_called()


    def test_phase2_status_renders_exact_authorization_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = {"run_dir": root / "phase2-run"}
            current = {
                "record": {"epoch_number": 2},
                "digest": "2" * 64,
            }
            contract = {
                "protocol_sha256": "a" * 64,
                "model": "gpt-5.6",
                "reasoning_profile": "high",
                "max_total_tokens": 1000,
                "max_wall_time_seconds": 600,
            }
            with mock.patch.object(
                agentteam_module,
                "_read_current_gate_epoch",
                return_value=current,
            ), mock.patch.object(
                agentteam_module,
                "_phase2_live_authorization_contract",
                return_value=contract,
            ):
                command = (
                    agentteam_module._post_backlog_gate_next_action(
                        context,
                        {
                            "epoch_number": 2,
                            "gates": [
                                {
                                    "gate_id": "P2-08",
                                    "state": "passed",
                                },
                                {
                                    "gate_id": "P2-09",
                                    "state": (
                                        "awaiting_operator_authorization"
                                    ),
                                },
                            ],
                        },
                    )
                )
            self.assertIn("agentteam gate authorize", command)
            self.assertIn("--protocol-sha256 " + "a" * 64, command)
            self.assertIn("--model gpt-5.6", command)
            self.assertIn("--max-total-tokens 1000", command)
            self.assertIn("--approve", command)


    def test_readiness_capability_evidence_resolves_from_fixed_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            artifact_contents = {
                path: f"# {module}\n".encode("utf-8")
                for module, path in (
                    experiment_gates_module
                    ._CAPABILITY_TEST_ARTIFACT_PATHS.items()
                )
            }
            for relative_path, content in artifact_contents.items():
                path = repo / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add registry artifacts"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            candidate_commit = _git_head(repo)
            declaration = {
                "resolve_from_registry": {
                    "registry_version": (
                        experiment_gates_module
                        .READINESS_CAPABILITY_REGISTRY_VERSION
                    )
                }
            }
            blueprint = {
                "contract": {
                    "candidate_source_commit": candidate_commit,
                },
                "post_backlog_gates": [
                    {
                        "gate_id": "P2-08",
                        "controller_action_input": {
                            "action": "promote_readiness",
                            "configuration": {
                                "capability_evidence": declaration,
                            },
                        },
                    }
                ],
            }

            resolved, authority_bindings, registry_bindings = (
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=repo,
                )
            )
            evidence = resolved["post_backlog_gates"][0][
                "controller_action_input"
            ]["configuration"]["capability_evidence"]

            self.assertEqual(authority_bindings, [])
            self.assertEqual(len(registry_bindings), 1)
            self.assertEqual(registry_bindings[0]["capability_count"], 7)
            self.assertEqual(
                registry_bindings[0]["test_count"],
                sum(
                    len(test_ids)
                    for test_ids in (
                        experiment_gates_module
                        ._CAPABILITY_TEST_IDS.values()
                    )
                ),
            )
            self.assertEqual(
                tuple(
                    item["test_id"]
                    for item in evidence[
                        "machine_readable_result_bundle"
                    ]
                )[-2:],
                (
                    "tests.test_experiment_harness."
                    "TwoPhaseSchedulerExperimentBoundaryTests."
                    "test_provider_terminal_waits_one_tick_for_worker_outbox",
                    "tests.test_experiment_harness."
                    "TwoPhaseSchedulerExperimentBoundaryTests."
                    "test_terminal_without_worker_outbox_reconciles_on_second_tick",
                ),
            )
            for entries in evidence.values():
                for entry in entries:
                    self.assertEqual(
                        entry["sha256"],
                        hashlib.sha256(
                            artifact_contents[entry["artifact_path"]]
                        ).hexdigest(),
                    )

            for relative_path in artifact_contents:
                (repo / relative_path).write_text(
                    "dirty working tree\n",
                    encoding="utf-8",
                )
            resolved_again, _, bindings_again = (
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=repo,
                )
            )
            self.assertEqual(
                resolved_again["post_backlog_gates"][0][
                    "controller_action_input"
                ]["configuration"]["capability_evidence"],
                evidence,
            )
            self.assertEqual(bindings_again, registry_bindings)


    def test_readiness_registry_binding_fails_closed(self):
        blueprint = {
            "contract": {"candidate_source_commit": "0" * 40},
            "post_backlog_gates": [
                {
                    "gate_id": "P2-08",
                    "controller_action_input": {
                        "action": "promote_readiness",
                        "configuration": {
                            "capability_evidence": {
                                "resolve_from_registry": {
                                    "registry_version": "unknown.v1",
                                }
                            }
                        },
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "registry binding is invalid",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=Path(tmp),
                )


    def test_promotion_protocol_contract_uses_canonical_json_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "authority" / "protocol.json"
            _write_json(
                protocol_path,
                {
                    "schema_version": "fixture.v1",
                    "nested": {"value": 1},
                },
            )
            blueprint = {
                "contract": {"protocol_sha256": "0" * 64},
                "post_backlog_gates": [
                    {
                        "gate_id": "P2-08",
                        "controller_action_input": {
                            "action": "promote_readiness",
                            "configuration": {
                                "authority_artifacts": {
                                    "protocol_template": {
                                        "resolve_at_materialization": {
                                            "authority_root": "project_root",
                                            "relative_path": (
                                                "authority/protocol.json"
                                            ),
                                        }
                                    }
                                }
                            },
                        },
                    }
                ],
            }
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "canonical protocol template",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=root,
                )

            blueprint["contract"]["protocol_sha256"] = (
                taskpack_module._sha256_json(
                    json.loads(protocol_path.read_text(encoding="utf-8"))
                )
            )
            _, authority_bindings, registry_bindings = (
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=root,
                )
            )
            self.assertEqual(len(authority_bindings), 1)
            self.assertEqual(registry_bindings, [])


    def test_promotion_materialization_rejects_repeat_mode_order_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "authority" / "protocol.json"
            protocol = {
                "mode_order": [
                    "agentteam_direct",
                    "agentteam_full",
                    "single_codex",
                ],
                "repetition_policy": {"count": 2},
            }
            _write_json(protocol_path, protocol)
            blueprint = {
                "contract": {
                    "protocol_sha256": taskpack_module._sha256_json(
                        protocol
                    )
                },
                "post_backlog_gates": [
                    {
                        "gate_id": "P2-08",
                        "controller_action_input": {
                            "configuration": {
                                "authority_artifacts": {
                                    "protocol_template": {
                                        "resolve_at_materialization": {
                                            "authority_root": "project_root",
                                            "relative_path": (
                                                "authority/protocol.json"
                                            ),
                                        }
                                    }
                                }
                            }
                        },
                    },
                    {
                        "gate_id": "P2-09",
                        "controller_action_input": {
                            "configuration": {
                                "repeat_mode": "single_codex"
                            }
                        },
                    },
                ],
            }

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "counterbalanced protocol order",
            ):
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=root,
                )

            blueprint["post_backlog_gates"][1][
                "controller_action_input"
            ]["configuration"]["repeat_mode"] = "agentteam_full"
            resolved, _, _ = (
                taskpack_module._resolve_blueprint_controller_action_inputs(
                    blueprint,
                    project_root=root,
                )
            )
            self.assertEqual(
                resolved["post_backlog_gates"][1][
                    "controller_action_input"
                ]["configuration"]["repeat_mode"],
                "agentteam_full",
            )


    def test_concrete_readiness_capability_evidence_is_unchanged(self):
        concrete = {"existing": [{"test_id": "fixture"}]}
        blueprint = {
            "post_backlog_gates": [
                {
                    "gate_id": "P2-08",
                    "controller_action_input": {
                        "action": "promote_readiness",
                        "configuration": {
                            "capability_evidence": concrete,
                        },
                    },
                }
            ],
        }
        resolved, authority_bindings, registry_bindings = (
            taskpack_module._resolve_blueprint_controller_action_inputs(
                blueprint,
                project_root=Path.cwd(),
            )
        )
        self.assertEqual(
            resolved["post_backlog_gates"][0]["controller_action_input"]
            ["configuration"]["capability_evidence"],
            concrete,
        )
        self.assertEqual(authority_bindings, [])
        self.assertEqual(registry_bindings, [])


    def test_calibration_closure_rejects_source_snapshot_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            authority_root = Path(tmp) / "authority"
            authority_root.mkdir()
            request_path = authority_root / "request.json"
            request = {
                "schema_version": (
                    "phase2_deterministic_calibration_request.v1"
                ),
                "authority_roots": [str(authority_root)],
            }
            _write_json(request_path, request)
            expected_sha256 = hashlib.sha256(
                request_path.read_bytes()
            ).hexdigest()

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "authority root is unsafe",
            ):
                taskpack_module._snapshot_calibration_request_closure(
                    request_path,
                    authority_root / "generated-taskpack" / "snapshot",
                    expected_sha256,
                )


    def test_calibration_closure_digest_has_unambiguous_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            combined = root / "combined"
            split = root / "split"
            combined.mkdir()
            split.mkdir()
            encoded_second_entry = (
                len(b"b").to_bytes(8, "big") + b"b" + b"payload"
            )
            (combined / "a").write_bytes(encoded_second_entry)
            (split / "a").write_bytes(b"")
            (split / "b").write_bytes(b"payload")

            self.assertNotEqual(
                taskpack_module._digest_directory_tree(combined),
                taskpack_module._digest_directory_tree(split),
            )
            (combined / "link").symlink_to(combined / "a")
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "symlink or special file",
            ):
                taskpack_module._digest_directory_tree(combined)


    def test_calibration_closure_digest_normalizes_directory_and_file_modes(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            retained = root / "retained"
            (source / "nested").mkdir(parents=True)
            (source / "empty").mkdir()
            (source / "nested" / "authority.json").write_text(
                '{"authority":true}\n',
                encoding="utf-8",
            )
            (source / "nested").chmod(0o700)
            (source / "nested" / "authority.json").chmod(0o600)
            (retained / "nested").mkdir(parents=True)
            shutil.copyfile(
                source / "nested" / "authority.json",
                retained / "nested" / "authority.json",
            )
            (retained / "nested").chmod(0o500)
            (retained / "nested" / "authority.json").chmod(0o400)

            self.assertEqual(
                taskpack_module._digest_directory_tree(source),
                taskpack_module._digest_directory_tree(retained),
            )


    def test_follow_up_queue_next_text_includes_selected_provenance_and_readiness(self):
        from agentteam_runtime.follow_up_queue import (
            build_follow_up_queue_summary,
            render_follow_up_queue_text,
        )

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["在比赛 QEMU 环境复测端到端延迟。"],
                    "verification": ["python3 -m unittest test_taskpack.FollowUpQueue passed"],
                    "evidence_gaps": ["QEMU timing still pending."],
                },
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertIn("selected_item", summary)
        selected_item = summary["selected_item"]
        self.assertEqual(selected_item["source"], "report.next_steps")
        self.assertEqual(selected_item["source_taskpack_id"], "first-pass")
        self.assertEqual(
            selected_item["source_report_path"],
            "/tmp/first-pass/reports/final_report.md",
        )
        self.assertEqual(selected_item["readiness"], "review_needed")
        self.assertEqual(selected_item["blockers"], ["QEMU timing still pending."])
        self.assertEqual(
            selected_item["suggested_verification"],
            "python3 -m unittest test_taskpack.FollowUpQueue passed",
        )

        text = render_follow_up_queue_text(summary, next_only=True)

        self.assertIn("selected_source: report.next_steps", text)
        self.assertIn("selected_source_taskpack_id: first-pass", text)
        self.assertIn("selected_source_report: /tmp/first-pass/reports/final_report.md", text)
        self.assertIn("selected_readiness: review_needed", text)
        self.assertIn("selected_blockers: QEMU timing still pending.", text)
        self.assertIn(
            "selected_verification: python3 -m unittest test_taskpack.FollowUpQueue passed",
            text,
        )

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from agentteam_runtime.phase3_pilot_runner import (
    Phase3PilotRunner,
    Phase3PilotRunnerError,
    Phase3ProductionExecutor,
    _scored_candidate_patch_path,
    _usage_coverage_percent,
    build_phase3_experiment_protocol,
    materialize_phase3_runtime_taskpack,
)
from agentteam_runtime.experiment_modes import _integration_candidate_workspace
from agentteam_runtime.phase3_live_pilot import _live_sandbox_configuration
from agentteam_runtime.phase3_swe_evo_evaluator import (
    Phase3EvaluatorPatchRejected,
    Phase3SweEvoEvaluator,
    Phase3SweEvoEvaluatorError,
    _aggregate_upstream_report,
)
from agentteam_runtime.phase3_public_verification import (
    build_public_verification_environment,
)
from agentteam_runtime.experiment_sandbox import _candidate_source_unchanged
from agentteam_runtime.resource_envelope import approved_phase3_resource_envelope_binding
from agentteam_runtime.experiment_contract import canonical_json_sha256
from test_phase3_pilot import _build, _live_authorization


def _result(entry, *, resolved=True, tokens=100, wall=10.0, failure_class=None):
    return {
        "entry_id": entry["entry_id"],
        "terminal_status": "completed" if failure_class is None else "failed",
        "failure_class": failure_class,
        "usage": {
            "input_tokens": tokens - 20,
            "cached_input_tokens": 10,
            "output_tokens": 20,
            "reasoning_tokens": 0,
            "total_tokens": tokens,
            "coverage_percent": 100,
        },
        "wall_time_seconds": wall,
        "official_score": {
            "status": "completed",
            "resolved": resolved,
            "receipt_sha256": "a" * 64,
        },
    }


class _Executor:
    def __init__(self, result_factory=None):
        self.calls = []
        self.results = {}
        self.result_factory = result_factory or (lambda entry, attempt: _result(entry))

    def execute(self, entry, attempt_index):
        self.calls.append((copy.deepcopy(entry), attempt_index))
        result = self.result_factory(entry, attempt_index)
        self.results[entry["entry_id"]] = copy.deepcopy(result)
        return result

    def recover(self, entry, _attempt_index=0):
        return copy.deepcopy(self.results.get(entry["entry_id"]))


def _runner(root, executor):
    selection, preregistrations, contract = _build()
    return Phase3PilotRunner(
        root,
        pilot_id="fixture-pilot",
        pilot_contract=contract,
        live_authorization=_live_authorization(contract),
        selection=selection,
        preregistrations_by_instance=preregistrations,
        expected_epoch_number=1,
        expected_epoch_sha256="6" * 64,
        executor=executor,
    )


class Phase3PilotRunnerTests(unittest.TestCase):
    def test_candidate_source_comparison_ignores_untracked_evaluator_outputs(self):
        before = {
            "baseline_commit": "1" * 40,
            "baseline_tree": "2" * 40,
            "head_commit": "1" * 40,
            "head_tree": "2" * 40,
            "git_object_format": "sha1",
            "tracked_status_sha256": "3" * 64,
            "working_tree_sha256": "4" * 64,
        }
        after = dict(before)
        after["working_tree_sha256"] = "5" * 64

        self.assertTrue(_candidate_source_unchanged(before, after))
        after["tracked_status_sha256"] = "6" * 64
        self.assertFalse(_candidate_source_unchanged(before, after))

    def test_live_sandbox_mounts_codex_and_matching_code_mode_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary_root = root / "codex-release" / "bin"
            binary_root.mkdir(parents=True)
            codex = binary_root / "codex"
            host = binary_root / "codex-code-mode-host"
            codex.write_bytes(b"codex")
            host.write_bytes(b"host")

            with patch(
                "agentteam_runtime.phase3_live_pilot.shutil.which",
                return_value=str(codex),
            ):
                configuration = _live_sandbox_configuration(root / "pilot")

        views = {
            item["target"]: item["source"]
            for item in configuration["runtime_views"]
        }
        self.assertEqual(views["/opt/agentteam/bin/codex"], str(codex))
        self.assertEqual(
            views["/opt/agentteam/bin/codex-code-mode-host"],
            str(host),
        )
        self.assertEqual(
            configuration["environment"]["PYTHONDONTWRITEBYTECODE"],
            "1",
        )

    def test_runtime_candidate_falls_back_to_accepted_attempt_after_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runtime"
            taskpack_id = "direct-fixture"
            state_dir = run_root / taskpack_id / "state"
            state_dir.mkdir(parents=True)
            baseline = root / "baseline"
            attempt = root / "attempt"
            baseline.mkdir()
            attempt.mkdir()
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "integration_baseline": {
                            "integration_baseline_worktree_path": str(baseline)
                        },
                        "steps": [
                            {
                                "result": {
                                    "validation_status": "accepted",
                                    "worktree_path": str(attempt),
                                    "integration_verification_status": "failed",
                                }
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _integration_candidate_workspace(run_root, taskpack_id),
                str(attempt.resolve()),
            )

    def test_runtime_candidate_prefers_verified_integration_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runtime"
            taskpack_id = "direct-fixture"
            state_dir = run_root / taskpack_id / "state"
            state_dir.mkdir(parents=True)
            baseline = root / "baseline"
            attempt = root / "attempt"
            baseline.mkdir()
            attempt.mkdir()
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "integration_baseline": {
                            "integration_baseline_worktree_path": str(baseline)
                        },
                        "steps": [
                            {
                                "result": {
                                    "validation_status": "accepted",
                                    "worktree_path": str(attempt),
                                    "integration_verification_status": "passed",
                                }
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _integration_candidate_workspace(run_root, taskpack_id),
                str(baseline.resolve()),
            )

    def test_runtime_candidate_uses_latest_accepted_attempt_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runtime"
            taskpack_id = "direct-fixture"
            state_dir = run_root / taskpack_id / "state"
            state_dir.mkdir(parents=True)
            baseline = root / "baseline"
            first = root / "first"
            latest = root / "latest"
            for path in (baseline, first, latest):
                path.mkdir()
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "integration_baseline": {
                            "integration_baseline_worktree_path": str(baseline)
                        },
                        "steps": [
                            {
                                "result": {
                                    "validation_status": "accepted",
                                    "worktree_path": str(first),
                                    "integration_verification_status": "passed",
                                }
                            },
                            {
                                "result": {
                                    "validation_status": "accepted",
                                    "worktree_path": str(latest),
                                    "integration_verification_status": "failed",
                                }
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _integration_candidate_workspace(run_root, taskpack_id),
                str(latest.resolve()),
            )

    def test_swe_evo_evaluator_binds_gold_and_image_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arrow = root / "dataset.arrow"
            arrow.write_bytes(b"fixture")
            harness = root / "harness"
            harness.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=harness, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=harness,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=harness, check=True
            )
            (harness / "marker").write_text("fixed\n", encoding="utf-8")
            subprocess.run(["git", "add", "marker"], cwd=harness, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=harness, check=True)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=harness, text=True
            ).strip()
            row = {
                "instance_id": "fixture",
                "base_commit": "1" * 40,
                "patch": "gold",
                "test_patch": "tests",
                "all_patch": "all",
                "FAIL_TO_PASS": "[]",
                "PASS_TO_PASS": "[]",
            }
            bindings = {
                name + "_sha256": hashlib.sha256(row[name].encode()).hexdigest()
                for name in ("patch", "test_patch")
            }
            bindings.update(
                {
                    "all_patch_sha256": canonical_json_sha256(row["all_patch"]),
                    "fail_to_pass_sha256": canonical_json_sha256(row["FAIL_TO_PASS"]),
                    "pass_to_pass_sha256": canonical_json_sha256(row["PASS_TO_PASS"]),
                }
            )
            digest = "sha256:" + "a" * 64
            authority = {
                "fixture": {
                    "evaluator_only": {
                        "gold_bindings": bindings,
                        "image": {"reference": "registry/image", "manifest_digest": digest},
                    }
                }
            }
            evaluator = Phase3SweEvoEvaluator(
                arrow_path=arrow,
                arrow_sha256=hashlib.sha256(b"fixture").hexdigest(),
                harness_root=harness,
                harness_commit=commit,
                instances_by_id=authority,
                evaluator_root=root / "evaluator",
                resource_envelope_binding=approved_phase3_resource_envelope_binding(),
            )

            @dataclass
            class FakeSpec:
                instance_id: str
                namespace: str

            class FakeImage:
                attrs = {"RepoDigests": ["registry/image@" + "sha256:" + "a" * 64]}

            class FakeImages:
                @staticmethod
                def get(_key):
                    return FakeImage()

            class FakeClient:
                images = FakeImages()
                containers = object()
                api = object()

                @staticmethod
                def close():
                    return None

            def fake_run_instance(_spec, prediction, *_args, **_kwargs):
                run_id = _args[3]
                report_path = (
                    Path.cwd()
                    / "logs"
                    / "run_evaluation"
                    / run_id
                    / prediction["model_name_or_path"]
                    / "fixture"
                    / "report.json"
                )
                report_path.parent.mkdir(parents=True)
                report_path.write_text(
                    json.dumps(
                        {
                            "fixture": {
                                "patch_successfully_applied": True,
                                "resolved": True,
                                "tests_status": {
                                    "FAIL_TO_PASS": {
                                        "success": ["one"],
                                        "failure": ["two"],
                                    },
                                    "PASS_TO_PASS": {
                                        "success": ["three"],
                                        "failure": [],
                                    },
                                },
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                return {"completed": True, "resolved": True}

            modules = {
                "docker": type("Docker", (), {"DockerClient": lambda **_kwargs: FakeClient()}),
                "make_test_spec": lambda _row, namespace: FakeSpec("fixture", namespace),
                "run_instance": fake_run_instance,
            }
            candidate = root / "candidate.patch"
            candidate.write_text("diff --git a/a b/a\n", encoding="utf-8")
            with patch.object(evaluator, "_load_row", return_value=row), patch.object(
                evaluator, "_harness_modules", return_value=modules
            ):
                score = evaluator(
                    {
                        "instance_id": "fixture",
                        "entry_id": "entry",
                        "mode": "single_codex",
                    },
                    candidate,
                )

            self.assertTrue(score["resolved"])
            self.assertEqual(score["image_manifest_digest"], digest)
            self.assertEqual(score["swe_style_partial_score"], 0.65)

    def test_swe_evo_evaluator_rejects_gold_drift(self):
        row = {
            "patch": "changed",
            "test_patch": "tests",
            "all_patch": "all",
            "FAIL_TO_PASS": "[]",
            "PASS_TO_PASS": "[]",
        }
        bindings = {
            "patch_sha256": "0" * 64,
            "test_patch_sha256": hashlib.sha256(b"tests").hexdigest(),
            "all_patch_sha256": canonical_json_sha256("all"),
            "fail_to_pass_sha256": canonical_json_sha256("[]"),
            "pass_to_pass_sha256": canonical_json_sha256("[]"),
        }
        with self.assertRaisesRegex(Phase3SweEvoEvaluatorError, "gold binding"):
            Phase3SweEvoEvaluator._verify_gold_bindings(row, bindings)

    def test_swe_evo_evaluator_preflights_harness_dependencies(self):
        evaluator = object.__new__(Phase3SweEvoEvaluator)
        modules = {
            "docker": object(),
            "make_test_spec": object(),
            "run_instance": object(),
        }
        with patch.object(
            evaluator,
            "_harness_modules",
            return_value=modules,
        ) as load:
            receipt = evaluator.validate_environment()

        self.assertEqual(receipt["status"], "ready")
        self.assertEqual(
            receipt["required_modules"],
            ["docker", "make_test_spec", "run_instance"],
        )
        load.assert_called_once_with()

    def test_swe_evo_evaluator_preflight_rejects_incomplete_dependencies(self):
        evaluator = object.__new__(Phase3SweEvoEvaluator)
        with patch.object(
            evaluator,
            "_harness_modules",
            return_value={"docker": object()},
        ), self.assertRaisesRegex(
            Phase3SweEvoEvaluatorError,
            "dependencies are incomplete",
        ):
            evaluator.validate_environment()

    def test_patch_compatibility_accepts_non_overlapping_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator, row = self._patch_compatibility_fixture(root)
            candidate = root / "candidate.patch"
            candidate.write_text(
                """diff --git a/source.py b/source.py
index 77d2f6a..c19c212 100644
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-value = 1
+value = 2
""",
                encoding="ascii",
            )

            with patch.object(evaluator, "_load_row", return_value=row):
                receipt = evaluator.check_patch_compatibility(
                    {"instance_id": "fixture"}, candidate
                )

            self.assertEqual(receipt["status"], "compatible")
            self.assertIsNone(receipt["conflict_file"])

    def test_patch_compatibility_rejects_hidden_test_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator, row = self._patch_compatibility_fixture(root)
            candidate = root / "candidate.patch"
            candidate.write_text(
                """diff --git a/tests/test_source.py b/tests/test_source.py
index b859599..17fd4cf 100644
--- a/tests/test_source.py
+++ b/tests/test_source.py
@@ -1 +1 @@
-assert value == 1
+assert value in {1, 2}
""",
                encoding="ascii",
            )

            with patch.object(evaluator, "_load_row", return_value=row):
                with self.assertRaises(Phase3EvaluatorPatchRejected) as raised:
                    evaluator.check_patch_compatibility(
                        {"instance_id": "fixture"}, candidate
                    )

            receipt = raised.exception.receipt
            self.assertEqual(receipt["status"], "evaluator_patch_conflict")
            self.assertEqual(receipt["conflict_file"], "tests/test_source.py")
            self.assertEqual(receipt["conflict_line"], 1)
            self.assertNotIn("assert value != 2", json.dumps(receipt))

    def test_patch_compatibility_rejects_invalid_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator, row = self._patch_compatibility_fixture(root)
            candidate = root / "candidate.patch"
            candidate.write_text(
                """diff --git a/missing.py b/missing.py
index 77d2f6a..c19c212 100644
--- a/missing.py
+++ b/missing.py
@@ -1 +1 @@
-value = 1
+value = 2
""",
                encoding="ascii",
            )

            with patch.object(evaluator, "_load_row", return_value=row):
                with self.assertRaises(Phase3EvaluatorPatchRejected) as raised:
                    evaluator.check_patch_compatibility(
                        {"instance_id": "fixture"}, candidate
                    )

            self.assertEqual(
                raised.exception.receipt["status"], "candidate_patch_invalid"
            )

    @staticmethod
    def _patch_compatibility_fixture(root):
        repository = root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=repository, check=True
        )
        (repository / "source.py").write_text("value = 1\n", encoding="ascii")
        tests = repository / "tests"
        tests.mkdir()
        (tests / "test_source.py").write_text(
            "assert value == 1\n", encoding="ascii"
        )
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "fixture"], cwd=repository, check=True
        )
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
        ).strip()
        test_patch = """diff --git a/tests/test_source.py b/tests/test_source.py
index b859599..f06e5bb 100644
--- a/tests/test_source.py
+++ b/tests/test_source.py
@@ -1 +1,2 @@
 assert value == 1
+assert value != 2
"""
        row = {
            "patch": "gold",
            "test_patch": test_patch,
            "all_patch": "all",
            "FAIL_TO_PASS": "[]",
            "PASS_TO_PASS": "[]",
        }
        bindings = {
            "patch_sha256": hashlib.sha256(b"gold").hexdigest(),
            "test_patch_sha256": hashlib.sha256(test_patch.encode()).hexdigest(),
            "all_patch_sha256": canonical_json_sha256("all"),
            "fail_to_pass_sha256": canonical_json_sha256("[]"),
            "pass_to_pass_sha256": canonical_json_sha256("[]"),
        }
        evaluator = object.__new__(Phase3SweEvoEvaluator)
        evaluator.instances = {
            "fixture": {
                "worker_visible": {
                    "repository": {"commit": commit, "tree": tree}
                },
                "evaluator_only": {"gold_bindings": bindings},
            }
        }
        evaluator.repository_sources = {"fixture": repository.resolve()}
        evaluator.evaluator_root = root / "evaluator"
        evaluator.evaluator_root.mkdir()
        return evaluator, row

    def test_partial_score_does_not_reward_unapplied_patch(self):
        aggregate = _aggregate_upstream_report(
            json.dumps(
                {"fixture": {"patch_successfully_applied": False}}
            ).encode("ascii"),
            "fixture",
        )

        self.assertEqual(aggregate["swe_style_partial_score"], 0.0)

    def test_official_evaluator_runs_in_resource_leaf_and_seals_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate.patch"
            candidate.write_text("diff --git a/a b/a\n", encoding="ascii")
            evaluator = object.__new__(Phase3SweEvoEvaluator)
            evaluator.evaluator_root = root / "evaluator"
            evaluator.evaluator_root.mkdir()
            evaluator.timeout_seconds = 30
            evaluator.configuration = lambda: {"fixture": True}
            observed = {}

            class FakeHierarchy:
                def __init__(self, binding, **kwargs):
                    observed["binding"] = binding
                    observed["hierarchy_kwargs"] = kwargs

                def prepare(self, *, check_host):
                    observed["check_host"] = check_host

                def leaf_command(self, command, *, unit, evaluator):
                    observed["unit"] = unit
                    observed["evaluator"] = evaluator
                    return command

                def stop_transient_unit(self, unit):
                    observed["stopped"] = unit

            class FakeMonitor:
                def __init__(self, hierarchy, unit, *, scope, evaluator):
                    observed["monitor"] = (unit, scope, evaluator)

                def start(self):
                    return self

                def finish(self, *, binding, timed_out):
                    observed["finished"] = (binding, timed_out)
                    return {"scope": "evaluator", "status": "observed"}

                def cancel(self):
                    observed["cancelled"] = True

            score = {
                "schema_version": "phase3_official_score.v1",
                "status": "completed",
                "resolved": False,
            }

            def fake_runner(command, **kwargs):
                observed["command"] = command
                output = Path(command[command.index("--output") + 1])
                output.write_text(json.dumps(score), encoding="ascii")
                return subprocess.CompletedProcess(command, 0, "", "")

            evidence = root / "resource-evidence.json"
            result = evaluator.evaluate_resource_bound(
                {
                    "entry_id": "fixture-entry",
                    "instance_id": "fixture",
                    "mode": "single_codex",
                },
                candidate,
                resource_envelope_binding={"binding": "fixture"},
                resource_hierarchy_reference={
                    "run_id": "fixture-pilot",
                    "mode": "single_codex",
                },
                evidence_path=evidence,
                command_runner=fake_runner,
                hierarchy_factory=FakeHierarchy,
                monitor_factory=FakeMonitor,
            )

            self.assertEqual(result, score)
            self.assertTrue(observed["evaluator"])
            self.assertTrue(observed["command"][1].startswith("PYTHONPATH="))
            self.assertNotIn(
                hashlib.sha256(b"fixture-entry").hexdigest()[:16],
                observed["unit"],
            )
            self.assertEqual(observed["monitor"][1:], ("evaluator", True))
            self.assertEqual(json.loads(evidence.read_text())["scope"], "evaluator")

    def test_protocol_and_runtime_taskpack_bridge_are_executable(self):
        selection, preregistrations, _ = _build()
        instance_id = selection["ordered_instance_ids"][0]
        visible = preregistrations[instance_id]["authorization"][
            "equal_input_bindings"
        ]["shared_visible_inputs"]
        visible["task"]["acceptance_commands"] = [["pytest", "-q"]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            (repository / "fixture.py").write_text("value = 1\n", encoding="ascii")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            visible["repository"].update({"commit": commit, "tree": tree})
            visible_sha256 = canonical_json_sha256(visible)
            preregistrations[instance_id]["authorization"][
                "equal_input_bindings"
            ]["per_mode_visible_input_sha256"] = {
                mode: visible_sha256
                for mode in (
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                )
            }
            preregistrations[instance_id]["authorization_sha256"] = (
                canonical_json_sha256(preregistrations[instance_id]["authorization"])
            )
            evaluator = root / "visible-evaluator.py"
            evaluator.write_text("#!/usr/bin/python3\n", encoding="ascii")
            semantic = {
                "schema_version": "phase3_direct_taskpack.v1",
                "taskpack": {
                    "instance_id": instance_id,
                    "taskpack_id": "phase3-runtime-fixture",
                    "tasks": [
                        {
                            "objective": visible["task"]["goal"],
                            "acceptance_commands": [["pytest", "-q"]],
                        }
                    ],
                    "shared_budget": {"max_wall_time_seconds": 120},
                },
            }
            semantic["taskpack_sha256"] = canonical_json_sha256(semantic["taskpack"])
            dependency_root = root / "public-environment"
            (dependency_root / "bin").mkdir(parents=True)
            dependency_python = dependency_root / "bin" / "python3.9"
            dependency_python.write_text(
                "#!/bin/sh\nprintf 'Python 3.9.20\\n'\n",
                encoding="ascii",
            )
            dependency_python.chmod(0o755)
            public_environment = build_public_verification_environment(
                instance_id=instance_id,
                image_reference=(
                    "example.invalid/fixture@sha256:" + "a" * 64
                ),
                image_digest="sha256:" + "a" * 64,
                source_root=dependency_root,
            )

            protocol = build_phase3_experiment_protocol(
                instance_id=instance_id,
                preregistration=preregistrations[instance_id],
                repository_source=repository,
                common_evaluator_artifact=evaluator,
                public_verification_environment=public_environment,
            )
            first = materialize_phase3_runtime_taskpack(
                instance_id=instance_id,
                benchmark_taskpack=semantic,
                project_root=repository,
                output_root=root / "taskpack",
                model="gpt-test",
                public_verification_environment=public_environment,
            )
            replay = materialize_phase3_runtime_taskpack(
                instance_id=instance_id,
                benchmark_taskpack=semantic,
                project_root=repository,
                output_root=root / "taskpack",
                model="gpt-test",
                public_verification_environment=public_environment,
            )

            self.assertEqual(protocol["repository"]["source"], str(repository))
            self.assertEqual(protocol["seed"], 0)
            self.assertEqual(
                protocol["environment"]["tool_output_token_limit"],
                4_000,
            )
            self.assertEqual(
                protocol["environment"]["web_search_policy"],
                "disabled",
            )
            self.assertEqual(
                protocol["environment"]["model_auto_compact_token_limit"],
                32_768,
            )
            self.assertEqual(
                protocol["environment"]["tool_call_soft_limit"],
                12,
            )
            self.assertEqual(
                protocol["environment"]["tool_call_hard_limit"],
                16,
            )
            self.assertEqual(
                protocol["environment"]["tool_budget_policy"],
                "codex_pre_tool_budget.v1",
            )
            self.assertEqual(
                protocol["evaluator"]["candidate_patch_policy"],
                {
                    "submission": "production_with_supplemental_tests",
                    "test_change_handling": "retain_supplemental_unscored",
                    "test_path_classification": "public_conventional_test_paths.v1",
                    "hidden_test_composition": "require_clean_git_apply",
                    "candidate_invalid_outcome": "candidate_patch_invalid",
                    "conflict_outcome": "evaluator_patch_conflict",
                    "score_rejected_candidate": False,
                },
            )
            self.assertEqual(
                protocol["acceptance"]["command"][1:3],
                ["-m", "pytest"],
            )
            self.assertEqual(
                protocol["acceptance"]["command"][0],
                "/opt/agentteam/benchmark-env/bin/python3.9",
            )
            self.assertEqual(
                protocol["environment"]["dependency_cache_policy"],
                "declared_equal_read_only:"
                + public_environment["authority_sha256"],
            )
            self.assertEqual(first, replay)
            self.assertEqual(
                first["semantic_authority_sha256"],
                semantic["taskpack_sha256"],
            )
            frozen_taskpack = json.loads(
                (
                    Path(first["frozen_taskpack_dir"])
                    / "taskpack.yaml"
                ).read_text(encoding="utf-8")
            )
            self.assertIn(
                "supplemental verification evidence",
                frozen_taskpack["goal"],
            )
            frozen_verification = json.loads(
                (
                    Path(first["frozen_taskpack_dir"])
                    / "verification.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                frozen_verification["command"][:3],
                ["python3", "-m", "pytest"],
            )

    def test_scored_candidate_path_obeys_frozen_protocol_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            legacy = artifacts / "candidate.patch"
            scored = artifacts / "scored-candidate.patch"
            legacy.write_text("legacy\n", encoding="ascii")
            scored.write_text("scored\n", encoding="ascii")
            self.assertEqual(
                _scored_candidate_patch_path(
                    {
                        "evaluator": {
                            "candidate_patch_policy": {
                                "submission": "complete_candidate_patch"
                            }
                        }
                    },
                    root,
                ),
                legacy,
            )
            self.assertEqual(
                _scored_candidate_patch_path(
                    {
                        "evaluator": {
                            "candidate_patch_policy": {
                                "submission": (
                                    "production_with_supplemental_tests"
                                )
                            }
                        }
                    },
                    root,
                ),
                scored,
            )

    def test_production_executor_uses_protocol_budget_for_single_codex(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.protocols = {
            "fixture": {"budgets": {"max_wall_time_seconds": 1800}}
        }

        adapter = executor._adapter(
            {"instance_id": "fixture", "mode": "single_codex"}
        )

        self.assertEqual(adapter.provider.timeout_seconds, 1800)

    def test_production_executor_routes_instance_verification_authority(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.sandbox_configuration = {"legacy": True}
        executor.integration_verification_command = ["legacy"]
        executor.sandbox_configurations_by_instance = {
            "instance-a": {"environment": "a"},
        }
        executor.integration_verification_commands_by_instance = {
            "instance-a": ["/bound/python", "-c", "pass"],
        }
        entry = {"instance_id": "instance-a"}

        self.assertEqual(
            executor._sandbox_configuration(entry),
            {"environment": "a"},
        )
        self.assertEqual(
            executor._integration_verification_command(entry),
            ["/bound/python", "-c", "pass"],
        )

    def test_production_executor_skips_official_evaluator_after_infrastructure_failure(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.sandbox_configuration = {}
        executor.runtime_release = {}
        executor.resource_envelope_binding = None
        executor.resource_references = {}
        entry = {
            "entry_id": "fixture--r0--single_codex",
            "instance_id": "fixture",
            "mode": "single_codex",
            "repetition_index": 0,
        }
        expected = {"terminal_status": "infrastructure_failed"}

        with tempfile.TemporaryDirectory() as temporary:
            evaluator = Path(temporary) / "evaluator.py"
            evaluator.write_text("# fixture\n", encoding="ascii")
            executor.common_evaluator_artifact = str(evaluator)
            with patch.object(
                executor, "_allocation", return_value={"run_dir": temporary}
            ), patch.object(executor, "_adapter", return_value=object()), patch(
                "agentteam_runtime.phase3_pilot_runner.execute_bound_experiment_mode"
            ), patch(
                "agentteam_runtime.phase3_pilot_runner.load_experiment_result_bundle",
                return_value={"bundle": expected},
            ), patch.object(
                executor, "_terminal_result", return_value=expected
            ), patch.object(executor, "_evaluate_official") as official:
                result = executor.execute(entry, 0)

            self.assertEqual(result, expected)
            official.assert_not_called()

    def test_production_executor_seals_evaluator_failure_before_returning_usage(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.sandbox_configuration = {}
        executor.runtime_release = {}
        executor.resource_envelope_binding = None
        executor.resource_references = {}
        entry = {
            "entry_id": "fixture--r0--single_codex",
            "instance_id": "fixture",
            "mode": "single_codex",
            "repetition_index": 0,
        }
        expected = {"terminal_status": "completed"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator = root / "evaluator.py"
            evaluator.write_text("# fixture\n", encoding="ascii")
            executor.common_evaluator_artifact = str(evaluator)
            (root / "results").mkdir()
            with patch.object(
                executor, "_allocation", return_value={"run_dir": temporary}
            ), patch.object(executor, "_adapter", return_value=object()), patch(
                "agentteam_runtime.phase3_pilot_runner.execute_bound_experiment_mode"
            ), patch(
                "agentteam_runtime.phase3_pilot_runner.load_experiment_result_bundle",
                return_value={"bundle": expected},
            ), patch.object(
                executor,
                "_evaluate_official",
                side_effect=RuntimeError("evaluator timeout"),
            ), patch.object(
                executor, "_terminal_result", return_value=expected
            ) as terminal:
                result = executor.execute(entry, 0)

            failure = json.loads(
                (root / "results" / "official-evaluator-failure.json").read_text()
            )
            self.assertEqual(result, expected)
            self.assertEqual(failure["error_type"], "RuntimeError")
            self.assertGreaterEqual(failure["wall_time_seconds"], 0.0)
            terminal.assert_called_once_with(
                entry, root, None, evaluator_failed=True
            )

    def test_production_executor_seals_patch_rejection_without_scoring(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.sandbox_configuration = {}
        executor.runtime_release = {}
        executor.resource_envelope_binding = None
        executor.resource_references = {}
        executor.protocols = {
            "fixture": {
                "evaluator": {
                    "candidate_patch_policy": {
                        "submission": "production_with_supplemental_tests"
                    }
                }
            }
        }
        entry = {
            "entry_id": "fixture--r0--single_codex",
            "instance_id": "fixture",
            "mode": "single_codex",
            "repetition_index": 0,
        }
        receipt = {
            "schema_version": "phase3_patch_compatibility.v1",
            "status": "evaluator_patch_conflict",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "candidate_patch_sha256": "c" * 64,
            "test_patch_sha256": "d" * 64,
            "conflict_file": "tests/test_source.py",
            "conflict_line": 1,
            "diagnostic_sha256": "e" * 64,
        }

        class RejectingEvaluator:
            @staticmethod
            def check_patch_compatibility(_entry, _patch):
                raise Phase3EvaluatorPatchRejected(receipt)

            def __call__(self, _entry, _patch):
                raise AssertionError("rejected patch reached scoring")

        executor.official_evaluator = RejectingEvaluator()
        expected = {"terminal_status": "failed"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evaluator = root / "evaluator.py"
            evaluator.write_text("# fixture\n", encoding="ascii")
            executor.common_evaluator_artifact = str(evaluator)
            (root / "results").mkdir()
            (root / "artifacts").mkdir()
            (root / "artifacts" / "candidate.patch").write_text(
                "diff --git a/a b/a\n", encoding="ascii"
            )
            (root / "artifacts" / "scored-candidate.patch").write_text(
                "diff --git a/a b/a\n", encoding="ascii"
            )
            with patch.object(
                executor, "_allocation", return_value={"run_dir": temporary}
            ), patch.object(executor, "_adapter", return_value=object()), patch(
                "agentteam_runtime.phase3_pilot_runner.execute_bound_experiment_mode"
            ), patch(
                "agentteam_runtime.phase3_pilot_runner.load_experiment_result_bundle",
                return_value={"bundle": {"terminal_status": "completed"}},
            ), patch.object(
                executor, "_terminal_result", return_value=expected
            ) as terminal:
                result = executor.execute(entry, 0)

            rejection = json.loads(
                (root / "results" / "official-evaluator-rejection.json").read_text()
            )
            compatibility = json.loads(
                (root / "results" / "official-patch-compatibility.json").read_text()
            )

        self.assertEqual(result, expected)
        self.assertEqual(rejection["failure_class"], "evaluator_patch_conflict")
        self.assertEqual(compatibility, receipt)
        terminal.assert_called_once_with(
            entry, root, None, evaluator_rejected=True
        )

    def test_production_executor_rejects_ambiguous_evaluator_terminal(self):
        executor = object.__new__(Phase3ProductionExecutor)
        entry = {"entry_id": "fixture"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results").mkdir()
            (root / "results" / "official-score.json").write_text(
                "{}\n", encoding="ascii"
            )
            (root / "results" / "official-evaluator-rejection.json").write_text(
                "{}\n", encoding="ascii"
            )
            with patch.object(
                executor, "_allocation", return_value={"run_dir": temporary}
            ):
                with self.assertRaisesRegex(
                    Phase3PilotRunnerError,
                    "multiple official evaluator terminal artifacts",
                ):
                    executor.execute(entry, 0)

    def test_evaluator_failure_projection_preserves_usage_and_wall_time(self):
        executor = object.__new__(Phase3ProductionExecutor)
        entry = {"entry_id": "fixture--r0--single_codex"}
        bundle = {
            "terminal_status": "completed",
            "usage_totals": {
                "input_tokens": 980995,
                "cached_input_tokens": 903424,
                "output_tokens": 12897,
                "reasoning_tokens": 6127,
                "total_tokens": 993892,
            },
            "usage_coverage": {
                "covered_invocations": 1,
                "total_invocations": 1,
                "status": "complete",
            },
            "budget_result": {"elapsed_wall_time_seconds": 300.0},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results").mkdir()
            (root / "results" / "official-evaluator-failure.json").write_text(
                json.dumps({"wall_time_seconds": 1800.0}),
                encoding="ascii",
            )
            with patch(
                "agentteam_runtime.phase3_pilot_runner.load_experiment_result_bundle",
                return_value={"bundle": bundle},
            ):
                result = executor._terminal_result(
                    entry, root, None, evaluator_failed=True
                )

        self.assertEqual(result["terminal_status"], "infrastructure_failed")
        self.assertEqual(result["failure_class"], "evaluator_failure")
        self.assertEqual(result["usage"]["total_tokens"], 993892)
        self.assertEqual(result["usage"]["coverage_percent"], 100)
        self.assertEqual(result["wall_time_seconds"], 2100.0)

    def test_patch_conflict_projection_is_invalid_not_infrastructure(self):
        executor = object.__new__(Phase3ProductionExecutor)
        executor.protocols = {
            "fixture": {
                "evaluator": {
                    "candidate_patch_policy": {
                        "submission": "production_with_supplemental_tests",
                        "test_change_handling": "retain_supplemental_unscored",
                    }
                }
            }
        }
        entry = {
            "entry_id": "fixture--r0--single_codex",
            "instance_id": "fixture",
        }
        bundle = {
            "terminal_status": "completed",
            "usage_totals": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_tokens": 3,
                "total_tokens": 110,
            },
            "usage_coverage": {
                "covered_invocations": 1,
                "total_invocations": 1,
                "status": "complete",
            },
            "budget_result": {"elapsed_wall_time_seconds": 12.0},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results").mkdir()
            compatibility = {
                "schema_version": "phase3_patch_compatibility.v1",
                "status": "evaluator_patch_conflict",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "candidate_patch_sha256": "c" * 64,
                "test_patch_sha256": "d" * 64,
                "conflict_file": "tests/test_source.py",
                "conflict_line": 1,
                "diagnostic_sha256": "e" * 64,
            }
            (
                root / "results" / "official-patch-compatibility.json"
            ).write_text(json.dumps(compatibility), encoding="ascii")
            (root / "results" / "official-evaluator-rejection.json").write_text(
                json.dumps(
                    {
                        "schema_version": "phase3_official_evaluator_rejection.v1",
                        "status": "rejected",
                        "failure_class": "evaluator_patch_conflict",
                        "compatibility_sha256": canonical_json_sha256(
                            compatibility
                        ),
                        "wall_time_seconds": 0.5,
                    }
                ),
                encoding="ascii",
            )
            with patch(
                "agentteam_runtime.phase3_pilot_runner.load_experiment_result_bundle",
                return_value={"bundle": bundle},
            ):
                result = executor._terminal_result(
                    entry, root, None, evaluator_rejected=True
                )

        self.assertEqual(result["terminal_status"], "failed")
        self.assertEqual(result["failure_class"], "evaluator_patch_conflict")
        self.assertEqual(result["official_score"]["status"], "failed")
        self.assertEqual(result["wall_time_seconds"], 12.5)
        self.assertEqual(
            result["scored_candidate_relative_path"],
            "artifacts/scored-candidate.patch",
        )
        self.assertEqual(
            result["candidate_patch_policy"]["test_change_handling"],
            "retain_supplemental_unscored",
        )

    def test_sealed_usage_coverage_projects_to_percent(self):
        self.assertEqual(
            _usage_coverage_percent(
                {
                    "covered_invocations": 1,
                    "total_invocations": 1,
                    "status": "complete",
                }
            ),
            100,
        )
        self.assertEqual(
            _usage_coverage_percent(
                {
                    "covered_invocations": 1,
                    "total_invocations": 2,
                    "status": "incomplete",
                }
            ),
            50,
        )

    def test_full_provider_free_simulation_runs_all_counterbalanced_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor()
            runner = _runner(temporary, executor)

            state = runner.run()

            self.assertEqual(state["status"], "completed")
            expected = len(state["schedule"])
            self.assertEqual(expected, 12)
            self.assertEqual(len(executor.calls), expected)
            self.assertEqual(len(state["terminal_results"]), expected)
            self.assertEqual(
                state["aggregate_usage"]["total_tokens"],
                expected * 100,
            )
            first_instance = state["schedule"][0]["instance_id"]
            self.assertEqual(
                [
                    entry["mode"]
                    for entry in state["schedule"]
                    if entry["instance_id"] == first_instance
                ],
                [
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                    "agentteam_direct",
                    "agentteam_full",
                    "single_codex",
                ],
            )

    def test_third_repetition_is_triggered_by_outcome_or_variance(self):
        selection, _, _ = _build()
        first, second = selection["ordered_instance_ids"]

        def result_factory(entry, _attempt):
            if (
                entry["instance_id"] == first
                and entry["mode"] == "single_codex"
                and entry["repetition_index"] == 1
            ):
                return _result(entry, resolved=False)
            if (
                entry["instance_id"] == second
                and entry["mode"] == "agentteam_direct"
                and entry["repetition_index"] == 1
            ):
                return _result(entry, tokens=150)
            return _result(entry)

        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor(result_factory)
            state = _runner(temporary, executor).run()

        thirds = [
            entry
            for entry in state["schedule"]
            if entry["repetition_index"] == 2
        ]
        self.assertEqual(
            [(entry["instance_id"], entry["mode"]) for entry in thirds],
            [
                (first, "single_codex"),
                (second, "agentteam_direct"),
            ],
        )
        self.assertEqual(len(executor.calls), 14)

    def test_patch_rejection_does_not_trigger_quality_repetition(self):
        selection, _, _ = _build()
        rejected_instance = selection["ordered_instance_ids"][0]

        def result_factory(entry, _attempt):
            if (
                entry["instance_id"] == rejected_instance
                and entry["mode"] == "single_codex"
                and entry["repetition_index"] == 0
            ):
                result = _result(
                    entry,
                    resolved=False,
                    failure_class="evaluator_patch_conflict",
                )
                result["official_score"] = {
                    "status": "failed",
                    "resolved": False,
                    "reason": "evaluator_patch_conflict",
                }
                return result
            return _result(entry)

        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor(result_factory)
            state = _runner(temporary, executor).run()

        self.assertEqual(state["status"], "completed")
        self.assertFalse(
            any(
                entry["instance_id"] == rejected_instance
                and entry["mode"] == "single_codex"
                and entry["repetition_index"] == 2
                for entry in state["schedule"]
            )
        )
        self.assertEqual(len(executor.calls), 12)

    def test_resume_reuses_terminal_checkpoint_without_reexecution(self):
        with tempfile.TemporaryDirectory() as temporary:
            first_executor = _Executor()
            first = _runner(temporary, first_executor)
            state = first.run(max_executions=4)
            self.assertEqual(len(state["terminal_results"]), 4)

            second_executor = _Executor()
            resumed = _runner(temporary, second_executor).run()

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(len(second_executor.calls), 8)

    def test_resume_recovers_sealed_active_result_without_duplicate_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor()
            runner = _runner(temporary, executor)
            state = runner.initialize()
            entry = state["schedule"][0]
            state["active"] = {
                "entry_id": entry["entry_id"],
                "attempt_index": 0,
                "started_at": state["created_at"],
            }
            executor.results[entry["entry_id"]] = _result(entry)
            with runner._lease():
                runner._write_state(state)

            recovered = runner.run(max_executions=1)

        self.assertIn(entry["entry_id"], recovered["terminal_results"])
        self.assertNotEqual(executor.calls[0][0]["entry_id"], entry["entry_id"])

    def test_resume_recovers_stopped_executor_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor()
            runner = _runner(temporary, executor)
            state = runner.initialize()
            entry = state["schedule"][0]
            state.update(
                {
                    "status": "stopped",
                    "stop_reason": "executor_exception",
                    "active": {
                        "entry_id": entry["entry_id"],
                        "attempt_index": 0,
                        "started_at": state["created_at"],
                    },
                    "active_error": {
                        "type": "RuntimeError",
                        "message_sha256": "a" * 64,
                    },
                }
            )
            executor.results[entry["entry_id"]] = _result(entry)
            with runner._lease():
                runner._write_state(state)

            recovered = runner.run(max_executions=1)

        self.assertIn(entry["entry_id"], recovered["terminal_results"])
        self.assertNotEqual(recovered["stop_reason"], "executor_exception")
        self.assertNotIn("active_error", recovered)

    def test_ambiguous_active_result_stops_before_duplicate_provider_call(self):
        class NoRecoveryExecutor:
            def execute(self, entry, attempt_index):
                return _result(entry)

        with tempfile.TemporaryDirectory() as temporary:
            executor = NoRecoveryExecutor()
            runner = _runner(temporary, executor)
            state = runner.initialize()
            state["active"] = {
                "entry_id": state["schedule"][0]["entry_id"],
                "attempt_index": 0,
                "started_at": state["created_at"],
            }
            with runner._lease():
                runner._write_state(state)

            with self.assertRaisesRegex(
                Phase3PilotRunnerError,
                "cannot resume without recovery",
            ):
                runner.run()

    def test_retryable_failure_counts_usage_then_retries_once(self):
        def result_factory(entry, attempt):
            if attempt == 0:
                return _result(
                    entry,
                    resolved=False,
                    failure_class="provider_transport_error",
                )
            return _result(entry)

        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor(result_factory)
            state = _runner(temporary, executor).run(max_executions=2)

        entry_id = state["schedule"][0]["entry_id"]
        self.assertEqual(len(state["attempts"][entry_id]), 2)
        self.assertIn(entry_id, state["terminal_results"])
        self.assertEqual(state["aggregate_usage"]["total_tokens"], 200)

    def test_provider_infrastructure_failure_stops_without_retry(self):
        def result_factory(entry, _attempt):
            return _result(
                entry,
                resolved=False,
                failure_class="provider_infrastructure_error",
            )

        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor(result_factory)
            state = _runner(temporary, executor).run()

        self.assertEqual(state["status"], "stopped")
        self.assertEqual(
            state["stop_reason"], "provider_infrastructure_error"
        )
        self.assertEqual(len(executor.calls), 1)

    def test_patch_conflict_is_terminal_without_stopping_pilot(self):
        def result_factory(entry, _attempt):
            result = _result(
                entry,
                resolved=False,
                failure_class="evaluator_patch_conflict",
            )
            result["official_score"] = {
                "status": "failed",
                "resolved": False,
                "reason": "evaluator_patch_conflict",
            }
            return result

        with tempfile.TemporaryDirectory() as temporary:
            executor = _Executor(result_factory)
            state = _runner(temporary, executor).run(max_executions=1)

        self.assertEqual(state["status"], "running")
        self.assertIsNone(state["stop_reason"])
        self.assertEqual(len(state["terminal_results"]), 1)
        self.assertEqual(len(executor.calls), 1)

    def test_incomplete_usage_stops_pilot(self):
        def result_factory(entry, _attempt):
            result = _result(entry)
            result["usage"]["coverage_percent"] = 50
            return result

        with tempfile.TemporaryDirectory() as temporary:
            state = _runner(temporary, _Executor(result_factory)).run()

        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["stop_reason"], "incomplete_provider_usage")
        self.assertEqual(len(state["terminal_results"]), 1)

    def test_gold_shaped_result_is_rejected(self):
        def result_factory(entry, _attempt):
            result = _result(entry)
            result["official_score"]["test_patch"] = "secret"
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                Phase3PilotRunnerError,
                "gold content key",
            ):
                _runner(temporary, _Executor(result_factory)).run()

    def test_checkpoint_schedule_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = _runner(temporary, _Executor())
            state = runner.initialize()
            state["schedule"][0]["mode"] = "agentteam_full"
            with runner._lease():
                runner._write_state(state)

            with self.assertRaisesRegex(Phase3PilotRunnerError, "schedule drifted"):
                runner.status()


if __name__ == "__main__":
    unittest.main()

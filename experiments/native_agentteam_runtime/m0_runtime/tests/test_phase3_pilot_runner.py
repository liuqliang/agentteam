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
    build_phase3_experiment_protocol,
    materialize_phase3_runtime_taskpack,
)
from agentteam_runtime.experiment_modes import _integration_candidate_workspace
from agentteam_runtime.phase3_swe_evo_evaluator import (
    Phase3SweEvoEvaluator,
    Phase3SweEvoEvaluatorError,
    _aggregate_upstream_report,
)
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
            self.assertEqual(observed["monitor"][1:], ("evaluator", True))
            self.assertEqual(json.loads(evidence.read_text())["scope"], "evaluator")

    def test_protocol_and_runtime_taskpack_bridge_are_executable(self):
        selection, preregistrations, _ = _build()
        instance_id = selection["ordered_instance_ids"][0]
        visible = preregistrations[instance_id]["authorization"][
            "equal_input_bindings"
        ]["shared_visible_inputs"]
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
                            "acceptance_commands": [["python3", "-m", "unittest"]],
                        }
                    ],
                    "shared_budget": {"max_wall_time_seconds": 120},
                },
            }
            semantic["taskpack_sha256"] = canonical_json_sha256(semantic["taskpack"])

            protocol = build_phase3_experiment_protocol(
                instance_id=instance_id,
                preregistration=preregistrations[instance_id],
                repository_source=repository,
                common_evaluator_artifact=evaluator,
            )
            first = materialize_phase3_runtime_taskpack(
                instance_id=instance_id,
                benchmark_taskpack=semantic,
                project_root=repository,
                output_root=root / "taskpack",
                model="gpt-test",
            )
            replay = materialize_phase3_runtime_taskpack(
                instance_id=instance_id,
                benchmark_taskpack=semantic,
                project_root=repository,
                output_root=root / "taskpack",
                model="gpt-test",
            )

            self.assertEqual(protocol["repository"]["source"], str(repository))
            self.assertEqual(protocol["seed"], 0)
            self.assertEqual(first, replay)
            self.assertEqual(
                first["semantic_authority_sha256"],
                semantic["taskpack_sha256"],
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

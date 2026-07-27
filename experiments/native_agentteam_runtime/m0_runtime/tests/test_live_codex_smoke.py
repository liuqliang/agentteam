import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from agentteam_runtime.live_codex_smoke import (
    SUPPORTED_NON_WORKER_INVOCATION_INVENTORY,
)
from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    ProviderExecution,
)
from agentteam_runtime.phase1_usage_acceptance import (
    FIXED_ARTIFACT_RELATIVE,
    FAILURE_ARTIFACT_RELATIVE,
    Phase1UsageAcceptanceError,
    decode_bounded_provider_spool,
    run_acceptance,
)
from agentteam_runtime.usage_live_smoke import (
    FIXED_ACCEPTANCE_ARTIFACT,
    SUPPORTED_ACCEPTANCE_INVOCATION_INVENTORY,
    UsageLiveSmokeError,
    _provider_command,
    run_usage_live_smoke,
)


ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parents[1]


class LiveCodexSmokeTests(unittest.TestCase):
    def test_usage_live_smoke_resolves_codex_for_systemd_environment(self):
        with mock.patch(
            "agentteam_runtime.usage_live_smoke.shutil.which",
            return_value="/opt/codex/bin/codex",
        ):
            command = _provider_command(
                None,
                project_root=Path("/candidate"),
                result_path=Path("/output/result.json"),
                model=None,
            )
        self.assertEqual(command[0], "/opt/codex/bin/codex")
        self.assertEqual(command[1], "exec")

    def test_usage_live_smoke_rejects_missing_codex_before_launch(self):
        with mock.patch(
            "agentteam_runtime.usage_live_smoke.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(
                UsageLiveSmokeError,
                "Codex executable is unavailable",
            ):
                _provider_command(
                    None,
                    project_root=Path("/candidate"),
                    result_path=Path("/output/result.json"),
                    model=None,
                )

    def test_live_codex_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["expected_file"], "generated/live_codex_smoke.json")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "live_codex_smoke.json").exists()
            )
            controller_roots = list(
                (
                    output_dir
                    / "run"
                    / "state"
                    / "controller_invocations"
                    / "development_smoke"
                ).glob("DEVELOPMENT-SMOKE-SESSION-*")
            )
            self.assertEqual(len(controller_roots), 1)
            claim = json.loads(
                (controller_roots[0] / "controller_claim.json").read_text(
                    encoding="utf-8"
                )
            )
            invocation_dirs = list(
                (controller_roots[0] / "model_invocations").glob("INV-*")
            )
            self.assertEqual(len(invocation_dirs), 1)
            started = json.loads(
                (invocation_dirs[0] / "started.json").read_text(
                    encoding="utf-8"
                )
            )
            terminal = json.loads(
                (invocation_dirs[0] / "terminal.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(claim["usage_stage"], "development_smoke")
            self.assertEqual(started["usage_stage"], "development_smoke")
            self.assertEqual(
                started["runtime_execution_session_id"],
                claim["runtime_execution_session_id"],
            )
            self.assertEqual(
                terminal["terminal_writer"],
                "development_smoke_controller",
            )

    def test_supported_non_worker_inventory_matches_tracked_smoke_sources(self):
        runtime_dir = ROOT / "m0_runtime" / "agentteam_runtime"
        tracked_smokes = {
            path.name
            for path in runtime_dir.glob("live_codex*_smoke.py")
            if path.name != "usage_live_smoke.py"
        }
        inventory_smokes = {
            reference.split(":", 1)[0]
            for key, reference in SUPPORTED_NON_WORKER_INVOCATION_INVENTORY.items()
            if key.startswith("development_smoke_")
        }

        self.assertEqual(
            tracked_smokes,
            {
                "live_codex_smoke.py",
                "live_codex_scheduler_smoke.py",
                "live_codex_repo_context_smoke.py",
                "live_codex_pipeline_smoke.py",
                "live_codex_multifile_pipeline_smoke.py",
                "live_codex_cli_smoke.py",
            },
        )
        self.assertEqual(inventory_smokes, tracked_smokes)
        self.assertEqual(
            {
                "taskpack_author",
                "follow_up_author",
                "runtime_diagnostic",
            },
            set(SUPPORTED_NON_WORKER_INVOCATION_INVENTORY)
            - {
                key
                for key in SUPPORTED_NON_WORKER_INVOCATION_INVENTORY
                if key.startswith("development_smoke_")
            },
        )
        for filename in sorted(tracked_smokes):
            source = (runtime_dir / filename).read_text(encoding="utf-8")
            self.assertIn("DevelopmentSmokeCodexRuntimeAdapter", source)

    def test_live_codex_scheduler_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "scheduler-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_scheduler_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_scheduler_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "scheduler-smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex, changed_file="generated/live_codex_scheduler_smoke.json")
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_scheduler_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(
                summary["processed_task_ids"],
                ["TASK-LIVE-CODEX-SCHEDULER-SMOKE"],
            )
            self.assertTrue(summary["expected_file_exists"])
            self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["session_status"],
                "stopped",
            )

    def test_live_codex_cli_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "cli-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_cli_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_cli_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "cli-smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex, changed_file="generated/live_codex_cli_smoke.json")
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_cli_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(
                summary["processed_task_ids"],
                ["TASK-LIVE-CODEX-CLI-SMOKE"],
            )
            self.assertTrue(summary["expected_file_exists"])
            self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["runtime_adapter"],
                "CodexRuntimeAdapter",
            )
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["session_status"],
                "stopped",
            )

    def test_live_codex_repo_context_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "repo-context-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_repo_context_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_repo_context_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "repo-context-smoke"
            fake_codex = tmp_path / "fake_repo_context_codex.py"
            _write_fake_repo_context_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_repo_context_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["expected_selected_file"],
                "pkg/context_target.py",
            )
            self.assertEqual(
                summary["reported_selected_file"],
                "pkg/context_target.py",
            )
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["expected_file_exists"])

    def test_live_codex_pipeline_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "pipeline-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_pipeline_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "pipeline-smoke"
            fake_codex = tmp_path / "fake_pipeline_codex.py"
            _write_fake_pipeline_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["integration_queue_status"], "pending")
            self.assertEqual(summary["batch_status"], "verified")
            self.assertEqual(summary["verification_status"], "passed")
            self.assertEqual(summary["merge_status"], "merged")
            self.assertEqual(summary["changed_files"], ["src/text_utils.py"])
            self.assertEqual(summary["actual_changed_files"], ["src/text_utils.py"])
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["role_context_path"])
            self.assertTrue(summary["source_repo_tests_passed"])
            self.assertTrue(
                (Path(summary["repo_path"]) / "src" / "text_utils.py").exists()
            )

    def test_live_codex_multifile_pipeline_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "multifile-pipeline-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_multifile_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_multifile_pipeline_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "multifile-pipeline-smoke"
            fake_codex = tmp_path / "fake_multifile_pipeline_codex.py"
            _write_fake_multifile_pipeline_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_multifile_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            expected_files = ["docs/guide.md", "src/toc.py"]
            guide_text = (Path(summary["repo_path"]) / "docs" / "guide.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["integration_queue_status"], "pending")
            self.assertEqual(summary["batch_status"], "verified")
            self.assertEqual(summary["verification_status"], "passed")
            self.assertEqual(summary["merge_status"], "merged")
            self.assertEqual(sorted(summary["changed_files"]), expected_files)
            self.assertEqual(sorted(summary["actual_changed_files"]), expected_files)
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["role_context_path"])
            self.assertTrue(summary["source_repo_tests_passed"])
            self.assertIn("- [Install](#install)", guide_text)
            self.assertIn("  - [Linux Setup](#linux-setup)", guide_text)


class Phase1UsageAcceptanceTests(unittest.TestCase):
    def test_acceptance_inventory_completes_supported_path_matrix(self):
        self.assertEqual(
            SUPPORTED_ACCEPTANCE_INVOCATION_INVENTORY,
            {
                "acceptance_live_smoke": (
                    "usage_live_smoke.py:run_usage_live_smoke"
                )
            },
        )
        completed = {
            **SUPPORTED_NON_WORKER_INVOCATION_INVENTORY,
            **SUPPORTED_ACCEPTANCE_INVOCATION_INVENTORY,
        }
        self.assertIn("taskpack_author", completed)
        self.assertIn("follow_up_author", completed)
        self.assertIn("runtime_diagnostic", completed)
        self.assertEqual(
            {
                reference.split(":", 1)[0]
                for reference in completed.values()
                if reference.split(":", 1)[0].endswith("_smoke.py")
            },
            {
                "live_codex_smoke.py",
                "live_codex_scheduler_smoke.py",
                "live_codex_repo_context_smoke.py",
                "live_codex_pipeline_smoke.py",
                "live_codex_multifile_pipeline_smoke.py",
                "live_codex_cli_smoke.py",
                "usage_live_smoke.py",
            },
        )

    def test_bounded_helper_writes_only_provisional_external_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "acceptance-output"
            fake_codex = root / "codex"
            _write_usage_fake_codex(fake_codex)
            result = run_usage_live_smoke(
                project_root=REPO_ROOT,
                project="acceptance-project",
                run_id="acceptance-series-attempt-1",
                taskpack_id="phase1-model-invocation-usage",
                implementation_run_id="phase1-model-invocation-usage",
                gate_epoch=1,
                attempt_id="attempt-1",
                output_dir=output_dir,
                output=output_dir / "provisional.json",
                expected_commit=_git_head(REPO_ROOT),
                codex_command=[str(fake_codex)],
                systemd_runner_factory=_FakeGatedExecution,
            )

            self.assertEqual(result["terminal_status"], "completed")
            self.assertEqual(
                result["invocation_usage_record"]["usage_status"],
                "reported",
            )
            self.assertEqual(result["provider_totals"]["total_tokens"], 12)
            self.assertTrue((output_dir / "provisional.json").is_file())
            self.assertFalse(
                (output_dir / FIXED_ACCEPTANCE_ARTIFACT).exists(),
                "helper must not publish authoritative acceptance",
            )
            command = _FakeGatedExecution.last_command
            self.assertIn("read-only", command)
            self.assertIn("--json", command)

    def test_bounded_helper_rejects_output_inside_candidate(self):
        with self.assertRaisesRegex(
            UsageLiveSmokeError,
            "outside the candidate",
        ):
            run_usage_live_smoke(
                project_root=REPO_ROOT,
                project="acceptance-project",
                run_id="acceptance-series-attempt-1",
                taskpack_id="phase1-model-invocation-usage",
                implementation_run_id="phase1-model-invocation-usage",
                gate_epoch=1,
                attempt_id="attempt-1",
                output_dir=REPO_ROOT / ".acceptance-output",
                output=REPO_ROOT / ".acceptance-output" / "provisional.json",
            )

    def test_independent_decoder_rejects_malformed_mismatch_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "provider.jsonl"
            spool.write_text(
                '{"type":"turn.completed","usage":'
                '{"input_tokens":7,"cached_input_tokens":2,'
                '"output_tokens":5,"reasoning_tokens":3,'
                '"total_tokens":12}}\n',
                encoding="utf-8",
            )
            decoded = decode_bounded_provider_spool(spool)
            self.assertEqual(decoded["provider_totals"]["total_tokens"], 12)
            self.assertEqual(
                decoded["terminal_event_sha256"],
                hashlib.sha256(
                    json.dumps(
                        decoded["terminal_event"],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest(),
            )

            spool.write_text("{malformed}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                Phase1UsageAcceptanceError,
                "malformed provider JSONL",
            ):
                decode_bounded_provider_spool(spool)

            spool.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":7,'
                '"output_tokens":5,"total_tokens":"12"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Phase1UsageAcceptanceError,
                "total_tokens",
            ):
                decode_bounded_provider_spool(spool)

            spool.write_bytes(b" " * 33)
            with self.assertRaisesRegex(
                Phase1UsageAcceptanceError,
                "exceeds 32 bytes",
            ):
                decode_bounded_provider_spool(spool, maximum_bytes=32)

    def test_controller_pass_is_atomic_projected_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(Path(tmp))
            launches = []

            def helper(request):
                launches.append(request["run_id"])
                _write_acceptance_provisional(request)

            with _patched_acceptance_controller(fixture):
                result = run_acceptance(
                    **fixture["arguments"],
                    helper_runner=helper,
                )
                repeated = run_acceptance(
                    **fixture["arguments"],
                    helper_runner=helper,
                )

            final_path = Path(result["artifact_path"])
            artifact = json.loads(final_path.read_text(encoding="utf-8"))
            receipt = json.loads(
                Path(result["receipt_path"]).read_text(encoding="utf-8")
            )
            identity = json.loads(
                (
                    fixture["run_dir"]
                    / "state"
                    / "run_identity.v1.json"
                ).read_text(encoding="utf-8")
            )
            claim = json.loads(
                (
                    fixture["run_dir"]
                    / "state"
                    / "controller_claim.v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["projection"]["replay_stable"])
            self.assertEqual(result["projection"]["invocation_count"], 1)
            self.assertEqual(launches, [fixture["run_id"]])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(
                artifact["provider_totals"],
                {
                    "input_tokens": 7,
                    "cached_input_tokens": 2,
                    "output_tokens": 5,
                    "reasoning_tokens": 3,
                    "total_tokens": 12,
                },
            )
            for record in (
                artifact,
                artifact["invocation_start_record"],
                artifact["invocation_usage_record"],
                identity,
                claim,
            ):
                self.assertEqual(
                    record["implementation_run_id"],
                    fixture["implementation_run_id"],
                )
                self.assertEqual(record["gate_epoch"], 1)
            self.assertEqual(receipt["evidence_run_id"], fixture["run_id"])
            self.assertEqual(
                artifact["invocation_start_record"]["invocation_id"],
                artifact["invocation_usage_record"]["invocation_id"],
            )

    def test_controller_interrupted_publish_leaves_failure_without_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(Path(tmp))

            def interrupted(_path, _artifact):
                raise OSError("simulated publication interruption")

            with _patched_acceptance_controller(fixture):
                with self.assertRaisesRegex(
                    Phase1UsageAcceptanceError,
                    "simulated publication interruption",
                ):
                    run_acceptance(
                        **fixture["arguments"],
                        helper_runner=_write_acceptance_provisional,
                        final_publisher=interrupted,
                    )

            self.assertFalse(
                (fixture["run_dir"] / FIXED_ARTIFACT_RELATIVE).exists()
            )
            failure_path = fixture["run_dir"] / FAILURE_ARTIFACT_RELATIVE
            self.assertTrue(failure_path.is_file())
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "failed")
            self.assertIn("simulated publication interruption", failure["error"])

    def test_controller_requires_authorization_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            with self.assertRaisesRegex(
                Phase1UsageAcceptanceError,
                "authorize-live-call",
            ):
                run_acceptance(
                    profile_project_root=REPO_ROOT,
                    candidate_project_root=REPO_ROOT,
                    implementation_run_id="phase1-model-invocation-usage",
                    gate_epoch=1,
                    acceptance_series_id="phase1-acceptance",
                    attempt_id="attempt-1",
                    work_root=work_root,
                    expected_commit=_git_head(REPO_ROOT),
                    authorize_live_call=False,
                )
            self.assertFalse(work_root.exists())

    def test_controller_accepts_explicit_versioned_implementation_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(
                Path(tmp),
                namespace="v4",
            )
            with _patched_acceptance_controller(fixture):
                result = run_acceptance(
                    **fixture["arguments"],
                    helper_runner=_write_acceptance_provisional,
                )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                fixture["context"]["run_dir"],
                fixture["work_root"]
                / "runs"
                / "v4"
                / fixture["implementation_run_id"],
            )

    def test_controller_rejects_helper_hash_and_total_mismatches(self):
        for mismatch in ("hash", "totals"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as tmp:
                fixture = _acceptance_controller_fixture(Path(tmp))

                def mismatched_helper(request):
                    _write_acceptance_provisional(request)
                    path = Path(request["output"])
                    provisional = json.loads(path.read_text(encoding="utf-8"))
                    if mismatch == "hash":
                        provisional[
                            "provider_terminal_snapshot_sha256"
                        ] = "0" * 64
                    else:
                        provisional["provider_totals"]["total_tokens"] = 13
                    path.write_text(
                        json.dumps(provisional, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                with _patched_acceptance_controller(fixture):
                    with self.assertRaisesRegex(
                        Phase1UsageAcceptanceError,
                        "do(?:es)? not match independent decoder|hash mismatch",
                    ):
                        run_acceptance(
                            **fixture["arguments"],
                            helper_runner=mismatched_helper,
                        )
                self.assertFalse(
                    (fixture["run_dir"] / FIXED_ARTIFACT_RELATIVE).exists()
                )
                self.assertTrue(
                    (fixture["run_dir"] / FAILURE_ARTIFACT_RELATIVE).is_file()
                )

    def test_projection_rejection_does_not_publish_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(Path(tmp))
            with _patched_acceptance_controller(fixture), mock.patch(
                "agentteam_runtime.phase1_usage_acceptance."
                "_validate_isolated_projection",
                side_effect=Phase1UsageAcceptanceError(
                    "simulated projection rejection"
                ),
            ):
                with self.assertRaisesRegex(
                    Phase1UsageAcceptanceError,
                    "projection rejection",
                ):
                    run_acceptance(
                        **fixture["arguments"],
                        helper_runner=_write_acceptance_provisional,
                    )
            self.assertFalse(
                (fixture["run_dir"] / FIXED_ARTIFACT_RELATIVE).exists()
            )

    def test_failed_attempt_is_recovery_only_and_fresh_retry_can_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _acceptance_controller_fixture(root)

            def interrupted(_path, _artifact):
                raise OSError("simulated publication interruption")

            with _patched_acceptance_controller(fixture):
                with self.assertRaises(Phase1UsageAcceptanceError):
                    run_acceptance(
                        **fixture["arguments"],
                        helper_runner=_write_acceptance_provisional,
                        final_publisher=interrupted,
                    )
                with self.assertRaisesRegex(
                    Phase1UsageAcceptanceError,
                    "recovery-only",
                ):
                    run_acceptance(
                        **fixture["arguments"],
                        helper_runner=_write_acceptance_provisional,
                    )

            retry = dict(fixture)
            retry["arguments"] = {
                **fixture["arguments"],
                "attempt_id": "attempt-2",
            }
            retry["run_id"] = "phase1-acceptance-attempt-2"
            retry["run_dir"] = (
                fixture["work_root"] / "runs" / retry["run_id"]
            )
            launches = []

            def retry_helper(request):
                launches.append(request["run_id"])
                _write_acceptance_provisional(request)

            with _patched_acceptance_controller(retry):
                result = run_acceptance(
                    **retry["arguments"],
                    helper_runner=retry_helper,
                )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(launches, [retry["run_id"]])

    def test_concurrent_same_id_permits_exactly_one_provider_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(Path(tmp))
            entered = threading.Event()
            release = threading.Event()
            launches = []

            def helper(request):
                launches.append(request["run_id"])
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                _write_acceptance_provisional(request)

            with _patched_acceptance_controller(
                fixture,
                use_real_gate_locks=True,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    run_acceptance,
                    **fixture["arguments"],
                    helper_runner=helper,
                )
                self.assertTrue(entered.wait(timeout=5))
                second = executor.submit(
                    run_acceptance,
                    **fixture["arguments"],
                    helper_runner=helper,
                )
                second_result = second.result(timeout=5)
                release.set()
                first_result = first.result(timeout=10)

            self.assertEqual(first_result["status"], "passed")
            self.assertEqual(second_result["status"], "in_progress")
            self.assertEqual(launches, [fixture["run_id"]])

    def test_concurrent_different_attempts_share_one_epoch_gate_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _acceptance_controller_fixture(Path(tmp))
            entered = threading.Event()
            release = threading.Event()
            launches = []
            other_arguments = {
                **fixture["arguments"],
                "attempt_id": "attempt-2",
            }

            def helper(request):
                launches.append(request["run_id"])
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                _write_acceptance_provisional(request)

            with _patched_acceptance_controller(
                fixture,
                use_real_gate_locks=True,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    run_acceptance,
                    **fixture["arguments"],
                    helper_runner=helper,
                )
                self.assertTrue(entered.wait(timeout=5))
                second = executor.submit(
                    run_acceptance,
                    **other_arguments,
                    helper_runner=helper,
                )
                with self.assertRaisesRegex(
                    Phase1UsageAcceptanceError,
                    "gate mutation is active",
                ):
                    second.result(timeout=5)
                release.set()
                first_result = first.result(timeout=10)

            self.assertEqual(first_result["status"], "passed")
            self.assertEqual(launches, [fixture["run_id"]])


class _FakeGatedExecution:
    last_command = None

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
        self.command = list(command)
        self.cwd = cwd
        self.input_text = input_text
        self.timeout_seconds = timeout_seconds
        _FakeGatedExecution.last_command = self.command

    def prepare(self):
        return ExecutionGroupIdentity(
            gated_supervisor_pid=101,
            gated_supervisor_pgid=101,
            host_boot_id="11111111-2222-3333-4444-555555555555",
            gated_supervisor_start_ticks=10,
            launch_nonce_sha256="a" * 64,
            systemd_linger_enabled=True,
            systemd_transient_unit="agentteam-test.service",
            systemd_transient_invocation_id="b" * 32,
            systemd_transient_kill_mode="control-group",
            systemd_user_manager_identity="manager-test",
            systemd_transient_control_group="/user.slice/agentteam-test",
            systemd_user_service_invocation_id="c" * 32,
            systemd_user_service_control_group="/user.slice/user-service",
            systemd_user_service_kill_mode="control-group",
        )

    def permit_and_wait(self, **_kwargs):
        completed = subprocess.run(
            self.command,
            cwd=self.cwd,
            input=self.input_text,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.timeout_seconds,
        )
        return ProviderExecution(
            self.command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def cleanup_after_terminal(self):
        return None

    def abort_before_permit(self):
        return None


def _write_usage_fake_codex(path):
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "sys.stdin.read()",
                "output = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "output.parent.mkdir(parents=True, exist_ok=True)",
                "output.write_text('READY\\n', encoding='utf-8')",
                "print(json.dumps({",
                "    'type': 'turn.completed',",
                "    'provider_session_id': 'provider-session-1',",
                "    'provider_turn_id': 'provider-turn-1',",
                "    'usage': {",
                "        'input_tokens': 7,",
                "        'cached_input_tokens': 2,",
                "        'output_tokens': 5,",
                "        'reasoning_tokens': 3,",
                "        'total_tokens': 12,",
                "    },",
                "}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _acceptance_controller_fixture(root, *, namespace=None):
    work_root = root / "work"
    implementation_run_id = "phase1-model-invocation-usage"
    implementation_run_dir = work_root / "runs"
    if namespace is not None:
        implementation_run_dir /= namespace
    implementation_run_dir /= implementation_run_id
    implementation_run_dir.mkdir(parents=True)
    identity_path = implementation_run_dir / "state" / "run_identity.v1.json"
    identity_path.parent.mkdir()
    identity_path.write_text(
        json.dumps(
            {
                "schema_version": "run_identity.v1",
                "project_key": "acceptance-project",
                "run_id": implementation_run_id,
                "taskpack_id": implementation_run_id,
                "run_kind": "implementation",
                "creation_sequence": 1,
                "created_at": "2026-01-01T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    run_id = "phase1-acceptance-attempt-1"
    expected_commit = _git_head(REPO_ROOT)
    profile = {
        "profile_schema_version": "agentteam_profile.v1",
        "project_key": "acceptance-project",
        "work_root": str(work_root),
    }
    declaration = {
        "gate_id": "P1-LIVE",
        "evidence_artifact": (
            "acceptance/model-invocation-live-smoke.v1.json"
        ),
        "evidence_schema": (
            "experiments/native_agentteam_runtime/schemas/"
            "model_invocation_live_smoke.schema.json"
        ),
        "required_status_field": "controller_validation_status",
        "required_status_value": "passed",
        "commit_field": "validated_code_sha",
        "integration_head_relation": "ancestor_of",
    }
    context = {
        "profile": profile,
        "work_root": work_root,
        "project_root": REPO_ROOT,
        "run_dir": implementation_run_dir,
        "taskpack": {"taskpack_id": implementation_run_id},
        "declarations": [declaration],
        "declarations_by_id": {"P1-LIVE": declaration},
        "gate_root": (
            implementation_run_dir / "state" / "post_backlog_gates"
        ),
        "epochs_root": (
            implementation_run_dir
            / "state"
            / "post_backlog_gates"
            / "epochs"
        ),
        "locks_root": (
            implementation_run_dir
            / "state"
            / "post_backlog_gates"
            / "locks"
        ),
    }
    epoch = {
        "record": {
            "epoch_number": 1,
            "integration_head_sha": expected_commit,
            "git_object_format": "sha1",
        },
        "digest": "d" * 64,
    }
    arguments = {
        "profile_project_root": REPO_ROOT,
        "candidate_project_root": REPO_ROOT,
        "implementation_run_id": implementation_run_id,
        "implementation_run_dir": implementation_run_dir,
        "gate_epoch": 1,
        "acceptance_series_id": "phase1-acceptance",
        "attempt_id": "attempt-1",
        "work_root": work_root,
        "expected_commit": expected_commit,
        "authorize_live_call": True,
    }
    return {
        "work_root": work_root,
        "implementation_run_id": implementation_run_id,
        "run_id": run_id,
        "run_dir": work_root / "runs" / run_id,
        "profile": profile,
        "declaration": declaration,
        "context": context,
        "epoch": epoch,
        "arguments": arguments,
    }


@contextlib.contextmanager
def _patched_acceptance_controller(fixture, *, use_real_gate_locks=False):
    stable_snapshot = {
        "head": fixture["arguments"]["expected_commit"],
        "ordinary_status_sha256": "0" * 64,
        "full_status_sha256": "1" * 64,
    }
    patches = [
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_profile_runtime.load_project_profile",
            return_value=fixture["profile"],
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_gate_runtime._require_post_backlog_gate_context",
            return_value=fixture["context"],
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_gate_runtime._require_gate_declaration",
            return_value=fixture["declaration"],
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_gate_runtime._require_current_gate_epoch",
            return_value=fixture["epoch"],
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_gate_runtime._evaluate_one_post_backlog_gate",
            return_value={"state": "passed"},
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_require_same_git_repository",
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_verify_candidate_runtime_modules",
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance."
            "_require_candidate_epoch_binding",
        ),
        mock.patch(
            "agentteam_runtime.phase1_usage_acceptance._candidate_snapshot",
            return_value=stable_snapshot,
        ),
    ]
    if not use_real_gate_locks:
        patches.append(
            mock.patch(
                "agentteam_runtime.phase1_usage_acceptance."
                "_gate_runtime._gate_mutation_locks",
                side_effect=lambda _context, _gate_ids: (
                    contextlib.nullcontext()
                ),
            )
        )
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        yield


def _write_acceptance_provisional(request):
    output_dir = Path(request["output_dir"])
    invocation_id = f"INV-{request['run_id']}"
    invocation_dir = (
        output_dir / "lifecycle" / "model_invocations" / invocation_id
    )
    invocation_dir.mkdir(parents=True, exist_ok=True)
    terminal_event = {
        "type": "turn.completed",
        "provider_session_id": "provider-session-1",
        "provider_turn_id": "provider-turn-1",
        "usage": {
            "input_tokens": 7,
            "cached_input_tokens": 2,
            "output_tokens": 5,
            "reasoning_tokens": 3,
            "total_tokens": 12,
        },
    }
    spool_path = invocation_dir / "stdout.jsonl"
    spool_path.write_text(
        json.dumps(terminal_event, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    started_at = "2026-07-26T00:00:00Z"
    finished_at = "2026-07-26T00:00:01Z"
    start = {
        "start_schema_version": "model_invocation_started.v1",
        "invocation_id": invocation_id,
        "project": request["profile"]["project_key"],
        "run_id": request["run_id"],
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": request["taskpack_id"],
        "implementation_run_id": request["implementation_run_id"],
        "gate_epoch": request["gate_epoch"],
        "task_id": "P1-LIVE",
        "attempt_id": request["attempt_id"],
        "runtime_execution_session_id": "ACCEPTANCE-SESSION-1",
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": "ACCEPTANCE-OWNER-1",
        "agent_id": "phase1-usage-acceptance-controller",
        "role": "acceptance_live_smoke",
        "usage_stage": "acceptance_live_smoke",
        "backend": "codex",
        "model": "gpt-acceptance",
        "coverage_class": "supported_model_invocation",
        "gated_supervisor_pid": 101,
        "gated_supervisor_pgid": 101,
        "host_boot_id": "11111111-2222-3333-4444-555555555555",
        "gated_supervisor_start_ticks": 10,
        "launch_nonce_sha256": "a" * 64,
        "systemd_linger_enabled": True,
        "systemd_transient_unit": "agentteam-test.service",
        "systemd_transient_invocation_id": "b" * 32,
        "systemd_transient_kill_mode": "control-group",
        "systemd_user_manager_identity": "manager-test",
        "systemd_transient_control_group": "/user.slice/agentteam-test",
        "systemd_user_service_invocation_id": "c" * 32,
        "systemd_user_service_control_group": "/user.slice/user-service",
        "systemd_user_service_kill_mode": "control-group",
        "started_at": started_at,
    }
    terminal = {
        "usage_schema_version": "model_invocation_usage.v1",
        "usage_event_id": f"USAGE-{request['run_id']}",
        "invocation_id": invocation_id,
        "project": request["profile"]["project_key"],
        "run_id": request["run_id"],
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": request["taskpack_id"],
        "implementation_run_id": request["implementation_run_id"],
        "gate_epoch": request["gate_epoch"],
        "task_id": "P1-LIVE",
        "attempt_id": request["attempt_id"],
        "runtime_execution_session_id": "ACCEPTANCE-SESSION-1",
        "provider_session_id": "provider-session-1",
        "provider_predecessor_invocation_id": None,
        "provider_turn_id": "provider-turn-1",
        "provider_predecessor_turn_id": None,
        "lifecycle_owner_token": "ACCEPTANCE-OWNER-1",
        "terminal_writer": "acceptance_controller",
        "agent_id": "phase1-usage-acceptance-controller",
        "role": "acceptance_live_smoke",
        "usage_stage": "acceptance_live_smoke",
        "backend": "codex",
        "model": "gpt-acceptance",
        "coverage_class": "supported_model_invocation",
        "terminal_status": "completed",
        "usage_status": "reported",
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": "provider_reported",
        "provider_usage_snapshot": None,
        "unavailable_reason": None,
        "input_tokens": 7,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_tokens": 3,
        "total_tokens": 12,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_time_seconds": 1.0,
        "source_artifact_path": (
            f"model_invocations/{invocation_id}/stdout.jsonl"
        ),
    }
    (invocation_dir / "started.json").write_text(
        json.dumps(start, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (invocation_dir / "terminal.json").write_text(
        json.dumps(terminal, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    provisional = {
        "schema_version": "model_invocation_live_smoke.provisional.v1",
        "project": request["profile"]["project_key"],
        "run_id": request["run_id"],
        "taskpack_id": request["taskpack_id"],
        "implementation_run_id": request["implementation_run_id"],
        "gate_epoch": request["gate_epoch"],
        "acceptance_attempt_id": request["attempt_id"],
        "candidate_commit_sha": request["expected_commit"],
        "candidate_runtime_root": str(request["candidate_runtime_root"]),
        "terminal_status": "completed",
        "invocation_start_record": start,
        "invocation_usage_record": terminal,
        "provider_terminal_snapshot_sha256": hashlib.sha256(
            json.dumps(
                terminal_event,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        "provider_totals": {
            "input_tokens": 7,
            "cached_input_tokens": 2,
            "output_tokens": 5,
            "reasoning_tokens": 3,
            "total_tokens": 12,
        },
        "bounded_raw_spool_path": spool_path.relative_to(
            output_dir
        ).as_posix(),
    }
    Path(request["output"]).parent.mkdir(parents=True, exist_ok=True)
    Path(request["output"]).write_text(
        json.dumps(provisional, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_head(path):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _write_fake_codex(path, changed_file="generated/live_codex_smoke.json"):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'mode': 'fake', 'saw_prompt': 'live_codex_smoke' in prompt}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'mode': 'fake'}",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_pipeline_codex(path):
    changed_file = "src/text_utils.py"
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "role_context_path = pathlib.Path(mailbox['payload']['role_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "selected_paths = [entry['path'] for entry in repo_context['selected_files']]",
                f"target = worktree / {changed_file!r}",
                "target.write_text('\\n'.join([",
                "    'import re',",
                "    '',",
                "    'def normalize_slug(text):',",
                "    \"    normalized = re.sub(r'[^a-z0-9]+', '-', text.lower())\",",
                "    \"    return normalized.strip('-')\",",
                "    '',",
                "]), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake_pipeline',",
                "        'selected_paths': selected_paths,",
                "        'repo_context_path': str(repo_context_path),",
                "        'role_context_path': str(role_context_path),",
                "    },",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_multifile_pipeline_codex(path):
    changed_files = ["docs/guide.md", "src/toc.py"]
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "role_context_path = pathlib.Path(mailbox['payload']['role_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "json.loads(role_context_path.read_text(encoding='utf-8'))",
                "selected_paths = [entry['path'] for entry in repo_context['selected_files']]",
                "toc_source = worktree / 'src' / 'toc.py'",
                "toc_source.write_text('\\n'.join([",
                "    'import re',",
                "    '',",
                "    'def _slugify(title):',",
                "    \"    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')\",",
                "    '',",
                "    'def build_toc(markdown_text):',",
                "    '    lines = []',",
                "    '    in_toc = False',",
                "    '    for raw_line in markdown_text.splitlines():',",
                "    '        line = raw_line.strip()',",
                "    \"        if line == '<!-- TOC:start -->':\",",
                "    '            in_toc = True',",
                "    '            continue',",
                "    \"        if line == '<!-- TOC:end -->':\",",
                "    '            in_toc = False',",
                "    '            continue',",
                "    '        if in_toc:',",
                "    '            continue',",
                "    \"        if raw_line.startswith('## '):\",",
                "    '            title = raw_line[3:].strip()',",
                "    '            lines.append(f\\'- [{title}](#{_slugify(title)})\\')',",
                "    \"        elif raw_line.startswith('### '):\",",
                "    '            title = raw_line[4:].strip()',",
                "    '            lines.append(f\\'  - [{title}](#{_slugify(title)})\\')',",
                "    '    return lines',",
                "    '',",
                "]), encoding='utf-8')",
                "guide = worktree / 'docs' / 'guide.md'",
                "guide.write_text('\\n'.join([",
                "    '# Agent Guide',",
                "    '',",
                "    '<!-- TOC:start -->',",
                "    '- [Install](#install)',",
                "    '  - [Linux Setup](#linux-setup)',",
                "    '- [Usage Tips](#usage-tips)',",
                "    '<!-- TOC:end -->',",
                "    '',",
                "    '## Install',",
                "    '',",
                "    'Install the tool locally.',",
                "    '',",
                "    '### Linux Setup',",
                "    '',",
                "    'Use the standard Python runtime.',",
                "    '',",
                "    '## Usage Tips',",
                "    '',",
                "    'Keep generated artifacts small.',",
                "    '',",
                "]), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': {changed_files!r},",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake_multifile_pipeline',",
                "        'selected_paths': selected_paths,",
                "        'repo_context_path': str(repo_context_path),",
                "        'role_context_path': str(role_context_path),",
                "    },",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_repo_context_codex(path):
    changed_file = "generated/live_codex_repo_context_smoke.json"
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "selected_file = repo_context['selected_files'][0]['path']",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({",
                "    'repo_context_smoke': True,",
                "    'selected_file': selected_file,",
                "    'repo_context_path': str(repo_context_path),",
                "}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake',",
                "        'selected_file': selected_file,",
                "        'repo_context_path': str(repo_context_path),",
                "    }",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()

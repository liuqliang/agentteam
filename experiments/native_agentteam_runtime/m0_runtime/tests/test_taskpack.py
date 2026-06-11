import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import io
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from agentteam_runtime import (
    TaskpackValidationError,
    build_taskpack_runtime_args,
    draft_taskpack_files,
    draft_taskpack_from_goal,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
from agentteam_runtime.agentteam import (
    _build_project_authoring_summary,
    _build_run_status_summary,
    _canonical_run_dir,
    _handle_taskpack_new,
    _handle_run,
    _run_paths_for_frozen_taskpack,
    _set_taskpack_runtime_backend,
    _stop_authoring,
    _write_execution_result_text,
    _write_status_text,
)
from agentteam_runtime.completion_summary import (
    build_completion_summary,
    extend_completion_summary_lines,
)
from agentteam_runtime.diagnostic_chat import (
    build_runtime_diagnostic_context,
    render_runtime_diagnostic_context,
)
from agentteam_runtime.profile import build_project_profile, write_project_profile
from agentteam_runtime.taskpack_author import _command_list
from agentteam_runtime.taskpack_author import _canonicalize_codex_taskpack_files


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _test_env():
    import os

    env = os.environ.copy()
    runtime_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = runtime_root if not current else f"{runtime_root}:{current}"
    return env


def _arg_value(args, flag):
    index = args.index(flag)
    return args[index + 1]


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_agentteam_release_fixture(checkout, marker="fixture"):
    runtime_pkg = checkout / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    runtime_pkg.mkdir(parents=True, exist_ok=True)
    (runtime_pkg / "__init__.py").write_text(f"# {marker} runtime\n", encoding="utf-8")
    (checkout / "agentteam").write_text(
        f"#!/usr/bin/env python3\nprint({marker!r})\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"{marker} agentteam release"],
        cwd=checkout,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return _git_head(checkout)


def _git_head(repo):
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _read_jsonl(path):
    records = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class _WebhookCaptureHandler(BaseHTTPRequestHandler):
    payloads = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.payloads.append(json.loads(body))
        response = json.dumps({"code": 0, "msg": "success"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


def _start_webhook_capture_server():
    payloads = []
    handler = type("WebhookCaptureHandler", (_WebhookCaptureHandler,), {"payloads": payloads})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, payloads


def _write_failed_integration_run(run_dir):
    run_dir = Path(run_dir)
    integration_worktree = run_dir / "integration" / "optimize-pipeline"
    integration_worktree.mkdir(parents=True)
    (integration_worktree / "gesture_recognition").mkdir()
    (integration_worktree / "gesture_recognition" / "sim_eval.py").write_text(
        "# changed\n",
        encoding="utf-8",
    )
    failure_stderr = "\n".join(
        [
            "test_fast_path (test_sim_eval.SimEvalTest.test_fast_path) ... ok",
            "test_host_c_model_matches_exported_python_reference_exactly "
            "(test_c_algo.CAlgoTest.test_host_c_model_matches_exported_python_reference_exactly) ... FAIL",
            "",
            "======================================================================",
            "FAIL: test_host_c_model_matches_exported_python_reference_exactly "
            "(test_c_algo.CAlgoTest.test_host_c_model_matches_exported_python_reference_exactly)",
            "----------------------------------------------------------------------",
            "AssertionError: First differing element 346: 'pinch' != 'others'",
            "",
            "FAILED (failures=1)",
        ]
    )
    _write_json(
        run_dir / "state" / "integration_queue.json",
        {
            "queue_schema_version": "integration_queue.v1",
            "items": [
                {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "queue_status": "blocked",
                    "integration_status": "applied",
                    "integration_verification_status": "failed",
                    "integration_verification_exit_code": 1,
                    "integration_worktree_path": str(integration_worktree),
                }
            ],
        },
    )
    _write_json(
        run_dir / "codex_results" / "codex_result_optimize-pipeline-ATTEMPT-001.json",
        {
            "result_status": "completed",
            "changed_files": [
                "gesture_recognition/sim_eval.py",
                "gesture_recognition/tests/test_sim_eval.py",
            ],
            "output": {
                "operator_summary": {
                    "what_changed": "Optimized feature extraction and negative window selection.",
                    "verification_summary": "Local sim_eval tests passed.",
                }
            },
        },
    )
    _write_json(
        run_dir / "steps" / "STEP-0001-optimize-pipeline" / "backlog.json",
        {
            "items": [
                {
                    "task_id": "optimize-pipeline",
                    "title": "Optimize gesture evaluation pipeline",
                    "objective": "Improve Python evaluation speed without changing generated outputs.",
                    "write_scope": [
                        "gesture_recognition/sim_eval.py",
                        "gesture_recognition/tests/test_sim_eval.py",
                    ],
                }
            ]
        },
    )
    _write_jsonl(
        run_dir / "events.jsonl",
        [
            {
                "event_id": "EVT-0001",
                "event_type": "worker_result_recorded",
                "sequence": 1,
                "payload": {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "result_status": "completed",
                },
            },
            {
                "event_id": "EVT-0002",
                "event_type": "integration_verified",
                "sequence": 2,
                "payload": {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "integration_verification_status": "failed",
                    "integration_verification_exit_code": 1,
                    "integration_verification_stderr": failure_stderr,
                    "integration_verification_stdout": "",
                },
            },
        ],
    )
    return run_dir


def _write_completed_operator_run(run_dir):
    run_dir = Path(run_dir)
    operator_report = {
        "report_schema_version": "operator_run_report.v1",
        "task_count": 1,
        "blocked_count": 0,
        "task_reports": [
            {
                "task_id": "optimize-pipeline",
                "attempt_id": "optimize-pipeline-ATTEMPT-001",
                "status": "implementation completed",
                "what_changed": [
                    "Scanned the repository and implemented one evidence-backed optimization."
                ],
                "changed_files": ["gesture_recognition/sim_eval.py"],
                "verification": ["unit_tests: passed"],
                "integration": "passed",
                "merge_recommendation": "Review accepted patch before merging.",
                "next_steps": ["Run the full competition validation package."],
                "token_usage": {
                    "usage_status": "reported",
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "total_tokens": 1500,
                    "cached_input_tokens": None,
                    "reasoning_tokens": None,
                },
            }
        ],
        "token_usage": {
            "usage_status": "reported",
            "reported_attempt_count": 1,
            "unreported_attempt_count": 0,
            "input_tokens": 1200,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        },
    }
    _write_json(
        run_dir / "state" / "two_phase_scheduler_state.json",
        {
            "scheduler_status": "idle",
            "integration_baseline": {
                "integration_baseline_status": "ready",
                "integration_baseline_branch": "agentteam/run/taskpack-7/integration",
                "integration_baseline_worktree_path": str((run_dir / "integration-baseline").resolve()),
                "integration_baseline_head_sha": "abc123",
            },
            "backlog": {
                "items": [
                    {
                        "task_id": "optimize-pipeline",
                        "backlog_status": "done",
                    }
                ]
            },
            "steps": [],
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
                    "scheduler_status": "idle",
                    "operator_report": operator_report,
                },
            }
        ],
    )
    return run_dir


def _init_agentteam_profile_for_test(repo, work_root, project_key):
    completed = subprocess.run(
        [
            "python3",
            "-m",
            "agentteam_runtime.agentteam",
            "init",
            "--project-root",
            str(repo),
            "--project-key",
            project_key,
            "--work-root",
            str(work_root),
            "--author-runtime",
            "fake",
            "--runtime",
            "fake",
        ],
        env=_test_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed


def _start_fake_agentteam_run_for_test(repo, goal, taskpack_id):
    completed = subprocess.run(
        [
            "python3",
            "-m",
            "agentteam_runtime.agentteam",
            "start",
            "--project-root",
            str(repo),
            "--goal",
            goal,
            "--taskpack-id",
            taskpack_id,
            "--json",
        ],
        env=_test_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed


REPO_ROOT = Path(__file__).resolve().parents[4]


class TaskpackTests(unittest.TestCase):
    def test_completion_summary_reports_evidence_gaps_when_worker_omits_fields(self):
        summary = build_completion_summary(
            run_id="gap-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertIn("No changed files were reported.", summary["evidence_gaps"])
        self.assertIn("No verification evidence was reported.", summary["evidence_gaps"])
        self.assertIn("Evidence gaps:", lines)
        self.assertIn("- No verification evidence was reported.", lines)

    def test_execution_result_text_reports_followup_work_summary(self):
        result = {
            "status": "completed",
            "taskpack_id": "follow-up-run",
            "follow_up": {
                "source_taskpack_id": "previous-run",
                "source_report_path": "/tmp/previous-report.md",
            },
            "report": {
                "report_path": "/tmp/follow-up-report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["local benchmark: passed"],
                    "integration": "passed",
                    "integration_recommendation": "Run `agentteam integrate --taskpack follow-up-run`.",
                    "next_steps": ["Run the full competition validation package."],
                    "evidence_gaps": [],
                },
            },
            "paths": {"run_dir": "/tmp/follow-up-run"},
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn("source_taskpack_id: previous-run", output)
        self.assertIn("work_report: changed=Optimized the gesture scoring pipeline.", output)
        self.assertIn("files=gesture_recognition/sim_eval.py", output)
        self.assertIn("verification=local benchmark: passed", output)
        self.assertIn("integration=passed", output)
        self.assertIn("recommendation: merge=Run `agentteam integrate --taskpack follow-up-run`.", output)
        self.assertIn("next=Run the full competition validation package.", output)

    def test_runtime_diagnostic_context_summarizes_failed_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_failed_integration_run(Path(tmp) / "runs" / "taskpack-5")

            context = build_runtime_diagnostic_context(run_dir, topic="integration-failure")
            rendered = render_runtime_diagnostic_context(context)

            self.assertEqual(context["agent_role"], "runtime_diagnostic_agent")
            self.assertEqual(context["chat_status"], "context_ready")
            self.assertEqual(context["topic"], "integration-failure")
            self.assertEqual(context["latest_failure"]["task_id"], "optimize-pipeline")
            self.assertEqual(context["latest_failure"]["failed_test"], "test_host_c_model_matches_exported_python_reference_exactly")
            self.assertIn("First differing element 346", context["latest_failure"]["stderr_excerpt"])
            self.assertEqual(
                context["worker_results"][0]["changed_files"],
                [
                    "gesture_recognition/sim_eval.py",
                    "gesture_recognition/tests/test_sim_eval.py",
                ],
            )
            self.assertIn("runtime_diagnostic_agent", rendered)
            self.assertIn("test_host_c_model_matches_exported_python_reference_exactly", rendered)
            self.assertIn("Read-only role", rendered)

    def test_agentteam_cli_chat_prints_diagnostic_context_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_failed_integration_run(Path(tmp) / "runs" / "taskpack-5")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "chat",
                    "--run-dir",
                    str(run_dir),
                    "--topic",
                    "integration-failure",
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
            self.assertEqual(summary["chat_status"], "context_ready")
            self.assertEqual(summary["agent_role"], "runtime_diagnostic_agent")
            self.assertEqual(summary["latest_failure"]["failed_test"], "test_host_c_model_matches_exported_python_reference_exactly")

    def test_agentteam_cli_chat_interactive_launches_codex_tui_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = _write_failed_integration_run(tmp_path / "runs" / "taskpack-5")
            capture_path = tmp_path / "codex-argv.json"
            fake_codex = tmp_path / "fake_codex.py"
            fake_codex.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"Path({str(capture_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "chat",
                    "--run-dir",
                    str(run_dir),
                    "--topic",
                    "integration-failure",
                    "--interactive",
                    "--codex-command",
                    "python3",
                    str(fake_codex),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            argv = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertNotIn("exec", argv)
            self.assertEqual(_arg_value(argv, "-C"), str(run_dir.resolve()))
            self.assertEqual(_arg_value(argv, "-s"), "read-only")
            self.assertIn("--no-alt-screen", argv)
            self.assertIn("runtime_diagnostic_agent", argv[-1])
            self.assertIn("test_host_c_model_matches_exported_python_reference_exactly", argv[-1])

    def test_agentteam_cli_report_renders_operator_summary_and_writes_report_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_completed_operator_run(Path(tmp) / "runs" / "taskpack-7")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--run-dir",
                    str(run_dir),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("AgentTeam Run Report", completed.stdout)
            self.assertIn("Run: taskpack-7", completed.stdout)
            self.assertIn("Status: completed", completed.stdout)
            self.assertIn("Token usage: total=1500 input=1200 output=300 reported=1/1", completed.stdout)
            self.assertIn("## Operator Summary", completed.stdout)
            self.assertIn("What changed: Scanned the repository", completed.stdout)
            self.assertIn("Integration: passed", completed.stdout)
            self.assertIn("Integration recommendation: Review the final report, then run `agentteam integrate --taskpack taskpack-7` from a clean target repository if these changes should land.", completed.stdout)
            self.assertIn("Scanned the repository", completed.stdout)
            self.assertIn("gesture_recognition/sim_eval.py", completed.stdout)
            report_path = run_dir / "reports" / "final_report.md"
            self.assertTrue(report_path.exists())
            self.assertIn("Run: taskpack-7", report_path.read_text(encoding="utf-8"))
            self.assertIn("Tokens: total=1500 input=1200 output=300", report_path.read_text(encoding="utf-8"))
            report_json = json.loads((run_dir / "reports" / "final_report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                report_json["completion_summary"]["what_changed"],
                ["Scanned the repository and implemented one evidence-backed optimization."],
            )
            self.assertEqual(report_json["completion_summary"]["integration"], "passed")
            self.assertEqual(
                report_json["completion_summary"]["next_steps"],
                ["Run the full competition validation package."],
            )
            artifacts_root = Path(tmp) / "artifacts"
            self.assertTrue((artifacts_root / ".git").exists())
            self.assertTrue((artifacts_root / "runs" / "taskpack-7" / "reports" / "final_report.md").exists())
            tracked_completed = subprocess.run(
                ["git", "-C", str(artifacts_root), "ls-files"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(tracked_completed.returncode, 0, tracked_completed.stderr)
            self.assertIn(
                "runs/taskpack-7/reports/final_report.md",
                tracked_completed.stdout.splitlines(),
            )

    def test_install_local_replaces_existing_launcher_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agentteam"
            target.symlink_to(REPO_ROOT / "agentteam")
            env = {**os.environ, "HOME": str(home)}

            completed = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts" / "install-local.sh")],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(target.is_symlink())
            config = json.loads(
                (home / ".local" / "share" / "agentteam" / "launcher.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(config["development_repo_root"], str(REPO_ROOT))

    def test_agentteam_cli_submit_fake_one_shot_runs_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "taskpacks"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "submit",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Submit fake one-shot taskpack.",
                    "--work-root",
                    str(work_root),
                    "--taskpack-id",
                    "cli-submit-fake",
                    "--author-runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-submit-fake")
            self.assertEqual(summary["validation"]["status"], "accepted")
            self.assertEqual(summary["runtime"], "fake")
            self.assertTrue((work_root / "drafts" / "cli-submit-fake").exists())
            self.assertTrue((work_root / "frozen" / "cli-submit-fake" / "manifest.json").exists())
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                summary["run"]["snapshot"]["tasks"]["TASK-CLI_SUBMIT_FAKE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_submit_interactive_prompts_for_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "taskpacks"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "submit",
                    "--interactive",
                ],
                input="\n".join(
                    [
                        str(repo),
                        "Submit interactive fake taskpack.",
                        str(work_root),
                        "cli-submit-interactive",
                        "fake",
                        "auto",
                        "y",
                        "n",
                    ]
                )
                + "\n",
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertIn("Project root", completed.stderr)
            self.assertIn("Goal", completed.stderr)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-submit-interactive")
            self.assertEqual(summary["runtime"], "fake")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                summary["run"]["snapshot"]["tasks"]["TASK-CLI_SUBMIT_INTERACTIVE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_init_writes_project_profile_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "codex",
                    "--runtime",
                    "auto",
                    "--notification-project",
                    "fixture-project",
                    "--feishu-webhook-env",
                    "AGENTTEAM_FEISHU_FIXTURE_WEBHOOK",
                    "--feishu-signing-secret-env",
                    "AGENTTEAM_FEISHU_FIXTURE_SECRET",
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
            profile_path = repo / ".agentteam" / "profile.json"
            self.assertEqual(Path(summary["profile_path"]), profile_path)
            self.assertTrue(profile_path.exists())
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["profile_schema_version"], "agentteam_profile.v1")
            self.assertEqual(profile["project_key"], "fixture-project")
            self.assertEqual(profile["work_root"], str(work_root.resolve()))
            self.assertEqual(profile["author_runtime"], "codex")
            self.assertEqual(profile["default_runtime"], "auto")
            self.assertEqual(profile["notification_project"], "fixture-project")
            self.assertEqual(profile["feishu"]["webhook_env"], "AGENTTEAM_FEISHU_FIXTURE_WEBHOOK")
            self.assertEqual(profile["feishu"]["signing_secret_env"], "AGENTTEAM_FEISHU_FIXTURE_SECRET")
            serialized = json.dumps(profile, sort_keys=True)
            self.assertNotIn("https://open.feishu.cn", serialized)
            self.assertNotIn("secret-token", serialized)

    def test_agentteam_cli_init_writes_project_verification_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "benchmark-project",
                    "--work-root",
                    str(work_root),
                    "--verification-command-json",
                    json.dumps(["python3", "tools/check.py"]),
                    "--performance-command-json",
                    json.dumps(["python3", "tools/bench.py", "--json"]),
                    "--metric",
                    "accuracy",
                    "--metric",
                    "latency_ms",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            profile = json.loads((repo / ".agentteam" / "profile.json").read_text(encoding="utf-8"))
            verification_profile = profile["verification_profile"]
            self.assertEqual(
                verification_profile["correctness"]["command"],
                ["python3", "tools/check.py"],
            )
            self.assertEqual(
                verification_profile["performance"]["command"],
                ["python3", "tools/bench.py", "--json"],
            )
            self.assertEqual(verification_profile["performance"]["metrics"], ["accuracy", "latency_ms"])

    def test_agentteam_cli_init_text_is_concise_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "codex",
                    "--runtime",
                    "auto",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("init_status: initialized\n", completed.stdout)
            self.assertIn("project: fixture-project\n", completed.stdout)
            self.assertIn("profile_path:", completed.stdout)
            self.assertNotIn("{", completed.stdout)
            self.assertNotIn("profile_schema_version", completed.stdout)

    def test_agentteam_cli_doctor_reports_profile_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "doctor-project")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "doctor",
                    "--project-root",
                    str(repo),
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
            self.assertEqual(summary["doctor_status"], "passed")
            check_names = [check["name"] for check in summary["checks"]]
            self.assertIn("profile", check_names)
            self.assertIn("git_repository", check_names)
            self.assertIn("verification_profile", check_names)

    def test_agentteam_cli_gc_prunes_old_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-project")
            old_release = work_root / "releases" / "old-release"
            latest_release = work_root / "releases" / "latest-release"
            old_release.mkdir(parents=True)
            latest_release.mkdir(parents=True)
            _write_json(
                old_release / "manifest.json",
                {
                    "manifest_schema_version": "agentteam_release_manifest.v1",
                    "release_id": "old-release",
                    "release_root": str(old_release),
                    "installed_at": "2026-06-10T00:00:00Z",
                },
            )
            _write_json(
                latest_release / "manifest.json",
                {
                    "manifest_schema_version": "agentteam_release_manifest.v1",
                    "release_id": "latest-release",
                    "release_root": str(latest_release),
                    "installed_at": "2026-06-11T00:00:00Z",
                },
            )
            _write_json(
                work_root / "active_release.json",
                {
                    "pointer_schema_version": "agentteam_active_release.v1",
                    "release_id": "latest-release",
                    "release_root": str(latest_release),
                    "activated_at": "2026-06-11T00:00:00Z",
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--force",
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
            self.assertEqual(summary["gc_status"], "completed")
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["old-release"])
            self.assertFalse(old_release.exists())
            self.assertTrue(latest_release.exists())

    def test_agentteam_cli_notify_test_sends_feishu_message_from_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            server, payloads = _start_webhook_capture_server()
            try:
                env = _test_env()
                env["AGENTTEAM_FEISHU_TEST_WEBHOOK"] = (
                    f"http://127.0.0.1:{server.server_port}/hook"
                )
                init_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "init",
                        "--project-root",
                        str(repo),
                        "--project-key",
                        "notify-project",
                        "--work-root",
                        str(work_root),
                        "--author-runtime",
                        "fake",
                        "--runtime",
                        "fake",
                        "--notification-project",
                        "notify-project",
                        "--feishu-webhook-env",
                        "AGENTTEAM_FEISHU_TEST_WEBHOOK",
                    ],
                    env=env,
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
                        "notify",
                        "test",
                        "--project-root",
                        str(repo),
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["notify_status"], "sent")
            self.assertEqual(summary["provider"], "feishu")
            self.assertEqual(summary["project"], "notify-project")
            self.assertEqual(summary["webhook_env"], "AGENTTEAM_FEISHU_TEST_WEBHOOK")
            self.assertFalse(summary["signing_enabled"])
            self.assertEqual(len(payloads), 1)
            message = payloads[0]["content"]["text"]
            self.assertIn("[AgentTeam] run_completed", message)
            self.assertIn("Completion summary:", message)
            self.assertIn("AgentTeam notification test for notify-project.", message)
            self.assertIn("If you receive this message, Feishu notification delivery works.", message)

    def test_agentteam_cli_notify_run_completed_sends_existing_run_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "completed-run"
            _init_repo(repo)
            _write_completed_operator_run(run_dir)
            server, payloads = _start_webhook_capture_server()
            try:
                env = _test_env()
                env["AGENTTEAM_FEISHU_TEST_WEBHOOK"] = (
                    f"http://127.0.0.1:{server.server_port}/hook"
                )
                init_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "init",
                        "--project-root",
                        str(repo),
                        "--project-key",
                        "notify-run-project",
                        "--work-root",
                        str(work_root),
                        "--author-runtime",
                        "fake",
                        "--runtime",
                        "fake",
                        "--notification-project",
                        "notify-run-project",
                        "--feishu-webhook-env",
                        "AGENTTEAM_FEISHU_TEST_WEBHOOK",
                    ],
                    env=env,
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
                        "notify",
                        "run-completed",
                        "--project-root",
                        str(repo),
                        "--taskpack",
                        "completed-run",
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["notify_status"], "sent")
            self.assertEqual(summary["event_type"], "run_completed")
            self.assertEqual(summary["taskpack_id"], "completed-run")
            self.assertEqual(len(payloads), 1)
            message = payloads[0]["content"]["text"]
            self.assertIn("[AgentTeam] run_completed", message)
            self.assertIn("Scanned the repository and implemented one evidence-backed optimization.", message)
            self.assertIn("gesture_recognition/sim_eval.py", message)

    def test_agentteam_cli_notify_test_requires_configured_feishu_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
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
                    "missing-notify-project",
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
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
                    "notify",
                    "test",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            error = json.loads(completed.stderr)
            self.assertEqual(error["error"], "Feishu webhook env is not configured")

    def test_agentteam_cli_init_keeps_project_git_status_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "clean-profile",
                    "--author-runtime",
                    "codex",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertEqual(status.stdout, "")
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(".agentteam/", exclude)

    def test_agentteam_cli_start_uses_project_profile_to_submit_fake_taskpack(self):
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
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "auto",
                    "--verification-command-json",
                    json.dumps(["python3", "-c", "print('profile-check')"]),
                    "--performance-command-json",
                    json.dumps(["python3", "tools/bench.py", "--json"]),
                    "--metric",
                    "latency_ms",
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
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Start from project profile.",
                    "--taskpack-id",
                    "cli-start-profile",
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
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-start-profile")
            self.assertEqual(summary["runtime"], "fake")
            self.assertEqual(summary["profile"]["profile_path"], str((repo / ".agentteam" / "profile.json").resolve()))
            self.assertEqual(summary["paths"]["work_root"], str(work_root.resolve()))
            self.assertTrue((work_root / "drafts" / "cli-start-profile").exists())
            loaded = load_taskpack(work_root / "frozen" / "cli-start-profile")
            self.assertEqual(loaded["verification"]["command"], ["python3", "-c", "print('profile-check')"])
            self.assertEqual(loaded["verification"]["performance"]["metrics"], ["latency_ms"])
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            baseline_worktree = work_root / "runs" / "cli-start-profile" / "integration-baseline"
            self.assertTrue(baseline_worktree.exists())
            baseline_ref = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "rev-parse",
                    "agentteam/run/cli-start-profile/integration",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(baseline_ref.returncode, 0, baseline_ref.stderr)
            repo_status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(repo_status.returncode, 0, repo_status.stderr)
            self.assertEqual(repo_status.stdout, "")

    def test_agentteam_cli_paths_reports_run_artifact_and_baseline_locations(self):
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
                    "paths-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for paths.",
                    "--taskpack-id",
                    "paths-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)

            json_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            cwd_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
                    "--json",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            run_dir = work_root / "runs" / "paths-run"
            baseline_worktree = run_dir / "integration-baseline"
            self.assertEqual(summary["project"], "paths-project")
            self.assertEqual(summary["project_root"], str(repo.resolve()))
            self.assertEqual(summary["work_root"], str(work_root.resolve()))
            self.assertEqual(summary["latest_run"], "paths-run")
            self.assertEqual(summary["run_dir"], str(run_dir.resolve()))
            self.assertEqual(summary["artifacts_root"], str((work_root / "artifacts").resolve()))
            self.assertEqual(summary["final_report"], str((run_dir / "reports" / "final_report.md").resolve()))
            self.assertEqual(
                summary["integration_baseline"]["branch"],
                "agentteam/run/paths-run/integration",
            )
            self.assertEqual(
                summary["integration_baseline"]["worktree_path"],
                str(baseline_worktree.resolve()),
            )
            self.assertTrue(summary["integration_baseline"]["worktree_exists"])
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("project: paths-project\n", text_completed.stdout)
            self.assertIn("latest_run: paths-run\n", text_completed.stdout)
            self.assertIn("integration_baseline_branch: agentteam/run/paths-run/integration\n", text_completed.stdout)
            self.assertIn(str(baseline_worktree.resolve()), text_completed.stdout)
            self.assertEqual(cwd_completed.returncode, 0, cwd_completed.stderr)
            cwd_summary = json.loads(cwd_completed.stdout)
            self.assertEqual(cwd_summary["project_root"], str(repo.resolve()))

    def test_agentteam_cli_integrate_fast_forwards_verified_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "integrate-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for integration.",
                    "--taskpack-id",
                    "integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_worktree = work_root / "runs" / "integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nintegrated\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam integration fixture"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "integrate-run",
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
            self.assertEqual(summary["integrate_status"], "merged")
            self.assertEqual(summary["merge_status"], "fast_forward")
            self.assertEqual(summary["after_head"], baseline_head)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n\nintegrated\n")

    def test_agentteam_cli_integrate_rebases_diverged_baseline_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "rebase-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for rebase integration.",
                "rebase-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "rebase-integrate-run" / "integration-baseline"
            (baseline_worktree / "agentteam-result.txt").write_text("baseline change\n", encoding="utf-8")
            subprocess.run(["git", "add", "agentteam-result.txt"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam baseline change"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            (repo / "main-change.txt").write_text("main branch change\n", encoding="utf-8")
            subprocess.run(["git", "add", "main-change.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "main branch advanced"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "rebase-integrate-run",
                    "--rebase",
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
            self.assertEqual(summary["integrate_status"], "merged")
            self.assertEqual(summary["rebase_status"], "rebased")
            self.assertEqual(summary["merge_status"], "rebased_fast_forward")
            self.assertEqual((repo / "main-change.txt").read_text(encoding="utf-8"), "main branch change\n")
            self.assertEqual((repo / "agentteam-result.txt").read_text(encoding="utf-8"), "baseline change\n")
            self.assertEqual(summary["after_head"], summary["integration_baseline"]["head_sha"])

    def test_agentteam_cli_integrate_rebase_conflict_returns_blocked_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "conflict-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for conflict integration.",
                "conflict-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "conflict-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nbaseline change\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam conflicting baseline"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            (repo / "README.md").write_text("# fixture\n\nmain change\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "main conflicting change"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            before_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "conflict-integrate-run",
                    "--rebase",
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
            self.assertEqual(summary["integrate_status"], "blocked")
            self.assertEqual(summary["rebase_status"], "conflict")
            self.assertEqual(summary["merge_status"], "not_merged")
            self.assertEqual(summary["conflicted_files"], ["README.md"])
            after_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(after_head, before_head)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n\nmain change\n")
            baseline_status = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(baseline_status, "")

    def test_agentteam_cli_integrate_requires_clean_target_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "dirty-integrate-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for dirty integration.",
                    "--taskpack-id",
                    "dirty-integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_worktree = work_root / "runs" / "dirty-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nintegrated\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam dirty integration fixture"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            before_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            (repo / "local.txt").write_text("uncommitted\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "dirty-integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            error = json.loads(completed.stderr)
            self.assertEqual(error["error"], "target repository must be clean before integrate")
            after_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(after_head, before_head)

    def test_agentteam_cli_status_and_report_show_integration_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "baseline-visible"
            baseline_worktree = run_dir / "integration-baseline"
            _init_repo(repo)
            baseline_worktree.mkdir(parents=True)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "baseline-visible-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/baseline-visible/integration",
                        "integration_baseline_worktree_path": str(baseline_worktree.resolve()),
                        "integration_baseline_head_sha": "abc123",
                    },
                },
            )

            status_json = subprocess.run(
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
                check=False,
            )
            status_text = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            report_json = subprocess.run(
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
                check=False,
            )
            report_text = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_json.returncode, 0, status_json.stderr)
            status_summary = json.loads(status_json.stdout)
            self.assertEqual(
                status_summary["integration_baseline"]["branch"],
                "agentteam/run/baseline-visible/integration",
            )
            self.assertEqual(status_summary["integration_baseline"]["head_sha"], "abc123")
            self.assertEqual(status_text.returncode, 0, status_text.stderr)
            self.assertIn(
                "integration_baseline_branch: agentteam/run/baseline-visible/integration\n",
                status_text.stdout,
            )
            self.assertEqual(report_json.returncode, 0, report_json.stderr)
            report_summary = json.loads(report_json.stdout)
            self.assertEqual(
                report_summary["integration_baseline"]["branch"],
                "agentteam/run/baseline-visible/integration",
            )
            self.assertEqual(report_text.returncode, 0, report_text.stderr)
            self.assertIn("Integration baseline: agentteam/run/baseline-visible/integration", report_text.stdout)
            self.assertIn("Baseline head: abc123", report_text.stdout)

    def test_agentteam_cli_start_prints_progress_to_stderr(self):
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
                    "progress-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
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
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Show progress while starting.",
                    "--taskpack-id",
                    "cli-start-progress",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("status: completed\n", completed.stdout)
            self.assertIn("taskpack_id: cli-start-progress\n", completed.stdout)
            self.assertIn(
                "work_report: changed=Worker did not provide a natural-language change summary.",
                completed.stdout,
            )
            self.assertIn("integration=blocked", completed.stdout)
            self.assertIn(
                "recommendation: merge=Do not merge until integration passes.",
                completed.stdout,
            )
            self.assertIn("report:", completed.stdout)
            self.assertNotIn('"draft"', completed.stdout)
            self.assertLessEqual(len([line for line in completed.stdout.splitlines() if line.strip()]), 12)
            self.assertIn("[agentteam] profile loaded: progress-project", completed.stderr)
            self.assertIn("[agentteam] authoring taskpack with fake", completed.stderr)
            self.assertIn("[agentteam] draft accepted: cli-start-progress", completed.stderr)
            self.assertIn("[agentteam] frozen taskpack created: cli-start-progress", completed.stderr)
            self.assertIn("[agentteam] runtime started:", completed.stderr)
            self.assertIn("[agentteam] run idle", completed.stderr)

    def test_agentteam_cli_start_versions_trace_artifacts_without_worktrees(self):
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
                    "artifact-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
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
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a versioned trace snapshot.",
                    "--taskpack-id",
                    "trace-artifacts",
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
            snapshot = summary["artifact_snapshot"]
            artifacts_root = work_root / "artifacts"
            self.assertEqual(snapshot["snapshot_status"], "committed")
            self.assertEqual(snapshot["artifacts_root"], str(artifacts_root.resolve()))
            self.assertTrue((artifacts_root / ".git").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "reports" / "final_report.md").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "reports" / "final_report.json").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "events.jsonl").exists())
            state_snapshot = artifacts_root / "runs" / "trace-artifacts" / "state"
            self.assertTrue(
                (state_snapshot / "two_phase_scheduler_state.json").exists()
                or (state_snapshot / "scheduler_state.json").exists()
            )
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "taskpack" / "taskpack.yaml").exists())
            tracked_completed = subprocess.run(
                ["git", "-C", str(artifacts_root), "ls-files"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(tracked_completed.returncode, 0, tracked_completed.stderr)
            tracked_files = tracked_completed.stdout.splitlines()
            self.assertIn("runs/trace-artifacts/reports/final_report.md", tracked_files)
            self.assertIn("runs/trace-artifacts/taskpack/taskpack.yaml", tracked_files)
            self.assertFalse(any(path.startswith("runs/trace-artifacts/worktrees/") for path in tracked_files))
            self.assertFalse(any(path.startswith("runs/trace-artifacts/integration/") for path in tracked_files))
            self.assertTrue(snapshot["commit_sha"])
            self.assertIn("artifact_trace:", completed.stderr)

    def test_agentteam_cli_status_summarizes_latest_run(self):
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
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for status.",
                    "--taskpack-id",
                    "cli-status-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("project: status-project", status_completed.stdout)
            self.assertIn("latest_run: cli-status-run", status_completed.stdout)
            self.assertIn("overall_status: idle", status_completed.stdout)
            self.assertIn("run_status: idle", status_completed.stdout)
            self.assertIn("tasks: 1 done, 0 blocked", status_completed.stdout)
            self.assertIn("inflight: 0", status_completed.stdout)
            self.assertIn("manual_gates: 0", status_completed.stdout)
            self.assertIn(str((work_root / "runs" / "cli-status-run").resolve()), status_completed.stdout)

    def test_agentteam_cli_logs_tails_latest_run_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "1",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("run: logs-run", completed.stdout)
            self.assertIn("EVT-0001 run_completed", completed.stdout)

    def test_agentteam_cli_explain_status_describes_idle_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "explain-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "explain-project")
            _write_completed_operator_run(run_dir)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "explain-status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("overall_status: idle", completed.stdout)
            self.assertIn(
                "Explanation: no worker or authoring process is currently active.",
                completed.stdout,
            )

    def test_agentteam_cli_status_reports_inflight_and_stopped_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "inflight-run"
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
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "max_ticks_reached",
                        "backlog": {
                            "items": [
                                {
                                    "task_id": "optimize-pipeline",
                                    "backlog_status": "ready",
                                }
                            ]
                        },
                        "inflight_attempts": [
                            {
                                "task_id": "optimize-pipeline",
                                "attempt_id": "ATTEMPT-001",
                                "agent_id": "implementation-worker-1",
                            }
                        ],
                        "steps": [
                            {
                                "task_id": "completed-task",
                                "result": {
                                    "task_id": "completed-task",
                                    "attempt_id": "ATTEMPT-000",
                                    "runtime_output": {
                                        "usage": {
                                            "input_tokens": 700,
                                            "output_tokens": 200,
                                            "total_tokens": 900,
                                        }
                                    },
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "exit_code": -15,
                                "stopped_by": "terminated",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("latest_run: inflight-run", status_completed.stdout)
            self.assertIn("overall_status: max_ticks_reached", status_completed.stdout)
            self.assertIn("run_status: max_ticks_reached", status_completed.stdout)
            self.assertIn("tokens: total=900 input=700 output=200 reported=1/1", status_completed.stdout)
            self.assertIn("inflight: 1", status_completed.stdout)
            self.assertIn("workers: 1 stopped, 0 running, 0 quarantined", status_completed.stdout)
            self.assertIn(
                "last_worker: implementation-worker-1 stopped exit_code=-15 stopped_by=terminated",
                status_completed.stdout,
            )

    def test_agentteam_cli_status_reports_active_authoring_over_idle_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "previous-run"
            author_dir = work_root / "drafts" / ".follow-up-author"
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
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            _write_completed_operator_run(run_dir)
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "follow-up",
                    "pid": os.getpid(),
                    "started_at": "2026-06-11T00:00:00Z",
                    "updated_at": "2026-06-11T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("latest_run: previous-run", status_completed.stdout)
            self.assertIn("overall_status: authoring", status_completed.stdout)
            self.assertIn("run_status: idle", status_completed.stdout)
            self.assertIn("active_phase: authoring", status_completed.stdout)
            self.assertIn("active_authoring: follow-up", status_completed.stdout)

    def test_agentteam_cli_status_reports_running_stale_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stale-run"
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
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            json_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            self.assertEqual(summary["liveness_status"], "running-stale")
            self.assertEqual(summary["processes"]["live"], 0)
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("liveness: running-stale", text_completed.stdout)
            events = _read_jsonl(run_dir / "events.jsonl")
            stale_events = [
                event for event in events
                if event.get("event_type") == "run_stale_detected"
            ]
            self.assertEqual(len(stale_events), 1)
            self.assertEqual(stale_events[0]["payload"]["liveness_status"], "running-stale")
            self.assertEqual(stale_events[0]["payload"]["run_id"], "stale-run")

    def test_agentteam_cli_status_reports_running_alive_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "alive-run"
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
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "running",
                                "worker_pid": os.getpid(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            summary = json.loads(status_completed.stdout)
            self.assertEqual(summary["liveness_status"], "running-alive")
            self.assertEqual(summary["processes"]["live"], 1)

    def test_agentteam_cli_watch_prints_one_progress_line_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "runs" / "watch-run"
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            registry_path = run_dir / "state" / "worker_process_registry.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {"items": [{"task_id": "watch-task", "backlog_status": "ready"}]},
                        "inflight_attempts": [{"task_id": "watch-task", "attempt_id": "ATTEMPT-001"}],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            registry_path.write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            before_state = state_path.read_text(encoding="utf-8")
            before_registry = registry_path.read_text(encoding="utf-8")

            watch_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "watch",
                    "--run-dir",
                    str(run_dir),
                    "--interval",
                    "0",
                    "--max-lines",
                    "1",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(watch_completed.returncode, 0, watch_completed.stderr)
            self.assertEqual(len([line for line in watch_completed.stdout.splitlines() if line.strip()]), 1)
            self.assertIn("run=watch-run", watch_completed.stdout)
            self.assertIn("liveness=running-stale", watch_completed.stdout)
            self.assertEqual(state_path.read_text(encoding="utf-8"), before_state)
            self.assertEqual(registry_path.read_text(encoding="utf-8"), before_registry)

    def test_agentteam_cli_stop_marks_latest_run_stopped_and_writes_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stop-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
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
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {"items": [{"task_id": "optimize", "backlog_status": "ready"}]},
                        "inflight_attempts": [{"task_id": "optimize", "attempt_id": "ATTEMPT-001"}],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_pid": 999999999,
                                "worker_status": "running",
                                "stop_file": str(stop_file),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            self.assertIn("project: stop-project", stop_completed.stdout)
            self.assertIn("latest_run: stop-run", stop_completed.stdout)
            self.assertIn("stop_status: stopped", stop_completed.stdout)
            self.assertTrue(stop_file.exists())
            registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["registry_status"], "stopped")
            self.assertEqual(registry["workers"][0]["worker_status"], "stopped")
            self.assertEqual(registry["workers"][0]["stopped_by"], "stale_pid")
            state = json.loads((run_dir / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["scheduler_status"], "stopped")
            self.assertEqual(state["previous_scheduler_status"], "running")
            self.assertEqual(len(state["inflight_attempts"]), 1)

    def test_agentteam_cli_stop_stale_skips_live_registered_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "live-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
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
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_pid": os.getpid(),
                                "worker_status": "running",
                                "stop_file": str(stop_file),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "live-run",
                    "--stale",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            summary = json.loads(stop_completed.stdout)
            self.assertEqual(summary["stop_status"], "not_stale")
            self.assertFalse(stop_file.exists())
            registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["registry_status"], "running")
            self.assertEqual(registry["workers"][0]["worker_status"], "running")
            state = json.loads((run_dir / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["scheduler_status"], "running")

    def test_agentteam_cli_stop_stale_cleans_all_stale_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            stale_run = work_root / "runs" / "stale-run"
            live_run = work_root / "runs" / "live-run"
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
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for run_dir, worker_pid in [(stale_run, 999999999), (live_run, os.getpid())]:
                (run_dir / "state").mkdir(parents=True)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                    encoding="utf-8",
                )
                (run_dir / "state" / "worker_process_registry.json").write_text(
                    json.dumps(
                        {
                            "registry_status": "running",
                            "workers": [
                                {
                                    "worker_agent_id": "implementation-worker-1",
                                    "worker_pid": worker_pid,
                                    "worker_status": "running",
                                    "stop_file": str(run_dir / "workers" / "implementation-worker-1.stop"),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                    "--stale",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            summary = json.loads(stop_completed.stdout)
            self.assertEqual(summary["stop_status"], "stale_cleaned")
            self.assertEqual(summary["cleaned_count"], 1)
            stale_registry = json.loads(
                (stale_run / "state" / "worker_process_registry.json").read_text(encoding="utf-8")
            )
            live_registry = json.loads(
                (live_run / "state" / "worker_process_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stale_registry["registry_status"], "stopped")
            self.assertEqual(stale_registry["workers"][0]["worker_status"], "stopped")
            self.assertEqual(live_registry["registry_status"], "running")
            self.assertEqual(live_registry["workers"][0]["worker_status"], "running")

    def test_agentteam_cli_stop_terminates_registered_worker_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "live-stop-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
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
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                (run_dir / "state").mkdir(parents=True)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                    encoding="utf-8",
                )
                (run_dir / "state" / "worker_process_registry.json").write_text(
                    json.dumps(
                        {
                            "registry_status": "running",
                            "workers": [
                                {
                                    "worker_agent_id": "implementation-worker-1",
                                    "worker_pid": worker.pid,
                                    "worker_status": "running",
                                    "stop_file": str(stop_file),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                stop_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "stop",
                        "--project-root",
                        str(repo),
                        "--run-dir",
                        str(run_dir),
                        "--grace-seconds",
                        "1",
                        "--json",
                    ],
                    env=_test_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )

                self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
                summary = json.loads(stop_completed.stdout)
                self.assertEqual(summary["stop_status"], "stopped")
                self.assertTrue(stop_file.exists())
                worker.wait(timeout=5)
                registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
                self.assertEqual(registry["registry_status"], "stopped")
                self.assertEqual(registry["workers"][0]["worker_status"], "stopped")
                self.assertEqual(registry["workers"][0]["stopped_by"], "terminated")
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=5)

    def test_agentteam_cli_taskpack_list_shows_frozen_taskpacks_and_run_status(self):
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
                    "list-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create listed frozen taskpack.",
                    "--taskpack-id",
                    "listed-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            extra = draft_taskpack_from_goal(
                project_root=repo,
                goal="Create listed frozen taskpack without run.",
                draft_root=work_root / "drafts",
                author_runtime="fake",
                taskpack_id="listed-not-run",
            )
            freeze_taskpack(extra["taskpack_dir"], work_root / "frozen")

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            self.assertIn("project: list-project", list_completed.stdout)
            self.assertIn("frozen_count: 2", list_completed.stdout)
            self.assertIn("listed-run", list_completed.stdout)
            self.assertIn("run_status=idle", list_completed.stdout)
            self.assertIn("listed-not-run", list_completed.stdout)
            self.assertIn("run_status=not_run", list_completed.stdout)

    def test_agentteam_cli_taskpack_list_uses_liveness_aware_run_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stale-listed"
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
                    "list-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            taskpack = draft_taskpack_from_goal(
                project_root=repo,
                goal="Create stale listed frozen taskpack.",
                draft_root=work_root / "drafts",
                author_runtime="fake",
                taskpack_id="stale-listed",
            )
            freeze_taskpack(taskpack["taskpack_dir"], work_root / "frozen")
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            self.assertIn("stale-listed", list_completed.stdout)
            self.assertIn("run_status=running-stale", list_completed.stdout)

    def test_agentteam_cli_taskpack_delete_dry_run_reports_paths_without_mutating(self):
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
                    "delete-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for base in ["drafts", "frozen", "runs"]:
                path = work_root / base / "delete-me"
                path.mkdir(parents=True)
                (path / "marker.txt").write_text(base, encoding="utf-8")

            delete_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--dry-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(delete_completed.returncode, 0, delete_completed.stderr)
            summary = json.loads(delete_completed.stdout)
            self.assertEqual(summary["delete_status"], "dry_run")
            self.assertEqual(summary["skipped_run"], str((work_root / "runs" / "delete-me").resolve()))
            self.assertTrue((work_root / "drafts" / "delete-me").exists())
            self.assertTrue((work_root / "frozen" / "delete-me").exists())
            self.assertTrue((work_root / "runs" / "delete-me").exists())

    def test_agentteam_cli_taskpack_delete_requires_explicit_run_delete_and_force(self):
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
                    "delete-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for base in ["drafts", "frozen", "runs"]:
                path = work_root / base / "delete-me"
                path.mkdir(parents=True)
                (path / "marker.txt").write_text(base, encoding="utf-8")

            refused = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--force",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("run exists", refused.stderr)

            deleted = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--delete-run",
                    "--force",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            summary = json.loads(deleted.stdout)
            self.assertEqual(summary["delete_status"], "deleted")
            self.assertEqual(summary["deleted_count"], 3)
            self.assertFalse((work_root / "drafts" / "delete-me").exists())
            self.assertFalse((work_root / "frozen" / "delete-me").exists())
            self.assertFalse((work_root / "runs" / "delete-me").exists())

    def test_agentteam_cli_update_status_reports_releases_and_run_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "release-a"
            managed_run = work_root / "runs" / "managed-run"
            unmanaged_run = work_root / "runs" / "unmanaged-run"
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
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-a",
                        "release_root": str(release_root),
                        "source_root": str(tmp_path / "checkout"),
                    }
                ),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "release-a", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            for run_dir, release_id in [(managed_run, "release-a"), (unmanaged_run, None)]:
                (run_dir / "state").mkdir(parents=True)
                state = {"scheduler_status": "running", "inflight_attempts": []}
                if release_id:
                    state["runtime_release_id"] = release_id
                    state["runtime_release_root"] = str(release_root)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps(state),
                    encoding="utf-8",
                )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            summary = json.loads(status_completed.stdout)
            self.assertEqual(summary["update_status"], "status")
            self.assertEqual(summary["active_release"]["release_id"], "release-a")
            self.assertEqual(summary["known_releases"][0]["release_id"], "release-a")
            self.assertEqual(summary["latest_installed_release"]["release_id"], "release-a")
            self.assertEqual(summary["runs_by_release"]["release-a"], ["managed-run"])
            self.assertEqual(summary["unmanaged_runs"], ["unmanaged-run"])

    def test_agentteam_cli_update_activate_and_rollback_record_release_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            for release_id in ["release-a", "release-b"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                _write_json(
                    release_root / "manifest.json",
                    {
                        "manifest_schema_version": "agentteam_release_manifest.v1",
                        "release_id": release_id,
                        "release_root": str(release_root),
                        "source_root": str(tmp_path / "checkout" / release_id),
                        "installed_at": (
                            "2026-06-08T09:00:00Z"
                            if release_id == "release-a"
                            else "2026-06-08T10:00:00Z"
                        ),
                    },
                )
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": "release-a",
                    "release_root": str(work_root / "releases" / "release-a"),
                },
            )

            activate_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--activate",
                    "release-b",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            rollback_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--rollback",
                    "release-a",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(activate_completed.returncode, 0, activate_completed.stderr)
            self.assertEqual(rollback_completed.returncode, 0, rollback_completed.stderr)
            activate_summary = json.loads(activate_completed.stdout)
            rollback_summary = json.loads(rollback_completed.stdout)
            self.assertEqual(activate_summary["release_event"]["event_type"], "update_activated")
            self.assertEqual(activate_summary["release_event"]["release_id"], "release-b")
            self.assertEqual(rollback_summary["release_event"]["event_type"], "rollback_activated")
            self.assertEqual(rollback_summary["release_event"]["release_id"], "release-a")
            release_events = _read_jsonl(work_root / "releases" / "events.jsonl")
            self.assertEqual(
                [event["event_type"] for event in release_events],
                ["update_activated", "rollback_activated"],
            )
            self.assertEqual([event["sequence"] for event in release_events], [1, 2])

    def test_agentteam_cli_update_status_text_lists_release_ids_only(self):
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
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for release_id in ["release-a", "release-b"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                            "installed_at": (
                                "2026-06-08T09:00:00Z"
                                if release_id == "release-a"
                                else "2026-06-08T10:00:00Z"
                            ),
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-b",
                        "release_root": str(work_root / "releases" / "release-b"),
                    }
                ),
                encoding="utf-8",
            )
            unmanaged_run = work_root / "runs" / "unmanaged-run"
            (unmanaged_run / "state").mkdir(parents=True)
            (unmanaged_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("active_release: release-b\n", status_completed.stdout)
            self.assertIn("latest_installed_release: release-b\n", status_completed.stdout)
            self.assertIn("active_is_latest: true\n", status_completed.stdout)
            self.assertIn(
                "known_releases:\n  - release-a\n  - release-b\n",
                status_completed.stdout,
            )
            self.assertNotIn("active_release_root", status_completed.stdout)
            self.assertNotIn("unmanaged_runs", status_completed.stdout)
            self.assertNotIn(str(work_root), status_completed.stdout)

    def test_agentteam_cli_update_from_git_installs_global_release_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            source_commit = _write_agentteam_release_fixture(checkout, "git-fixture")
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            release = summary["release"]
            release_id = release["release_id"]
            release_root = Path(release["release_root"])
            self.assertEqual(summary["update_status"], "installed")
            self.assertEqual(release["manifest_schema_version"], "agentteam_release_manifest.v2")
            self.assertEqual(release["install_method"], "git_ref")
            self.assertEqual(release["source_repo"], str(checkout.resolve()))
            self.assertEqual(release["source_ref"], "HEAD")
            self.assertEqual(release["source_commit"], source_commit)
            self.assertEqual(release_root.parent.parent, global_store.resolve())
            self.assertEqual(summary["active_release"]["release_id"], release_id)
            self.assertEqual(Path(summary["active_release"]["release_root"]), release_root)
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "m0_runtime"
                    / "agentteam_runtime"
                    / "__init__.py"
                ).exists()
            )
            self.assertTrue((work_root / "releases" / "refs" / f"{release_id}.json").exists())
            self.assertFalse((work_root / "releases" / release_id).exists())
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], release_id)
            self.assertEqual(Path(active["release_root"]), release_root)
            known_by_id = {item["release_id"]: item for item in summary["known_releases"]}
            self.assertIn(release_id, known_by_id)
            self.assertEqual(known_by_id[release_id]["source_commit"], source_commit)

    def test_agentteam_cli_update_from_git_reuses_release_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            first_commit = _write_agentteam_release_fixture(checkout, "first")
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            first = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            repeat = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            second_commit = _write_agentteam_release_fixture(checkout, "second")
            second = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    str(checkout),
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(repeat.returncode, 0, repeat.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            first_summary = json.loads(first.stdout)
            repeat_summary = json.loads(repeat.stdout)
            second_summary = json.loads(second.stdout)
            first_release = first_summary["release"]
            second_release = second_summary["release"]
            self.assertEqual(first_release["source_commit"], first_commit)
            self.assertEqual(repeat_summary["release"]["release_root"], first_release["release_root"])
            self.assertTrue(repeat_summary["release"]["reused_existing_release"])
            self.assertEqual(second_release["source_commit"], second_commit)
            self.assertNotEqual(second_release["release_id"], first_release["release_id"])
            self.assertNotEqual(second_release["release_root"], first_release["release_root"])

            rollback = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--rollback",
                    first_release["release_id"],
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            rollback_summary = json.loads(rollback.stdout)
            self.assertEqual(rollback_summary["active_release"]["release_id"], first_release["release_id"])
            self.assertEqual(rollback_summary["active_release"]["release_root"], first_release["release_root"])
            self.assertEqual(rollback_summary["release_event"]["event_type"], "rollback_activated")

    def test_agentteam_cli_update_from_git_installs_from_remote_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            bare_repo = tmp_path / "agentteam.git"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            source_commit = _write_agentteam_release_fixture(checkout, "remote-fixture")
            subprocess.run(
                ["git", "clone", "--bare", str(checkout), str(bare_repo)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            source_url = bare_repo.resolve().as_uri()
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            release = summary["release"]
            release_root = Path(release["release_root"])
            self.assertEqual(release["source_repo"], source_url)
            self.assertEqual(release["source_ref"], "HEAD")
            self.assertEqual(release["source_commit"], source_commit)
            self.assertEqual(release["install_method"], "git_ref")
            self.assertEqual(release_root.parent.parent, global_store.resolve())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue((work_root / "releases" / "refs" / f"{release['release_id']}.json").exists())
            self.assertFalse((work_root / "releases" / release["release_id"]).exists())

    def test_agentteam_cli_update_from_git_missing_remote_ref_keeps_active_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            bare_repo = tmp_path / "agentteam.git"
            work_root = tmp_path / "agentteam-work"
            global_store = tmp_path / "runtime-releases"
            _init_repo(repo)
            _init_repo(checkout)
            _write_agentteam_release_fixture(checkout, "remote-fixture")
            subprocess.run(
                ["git", "clone", "--bare", str(checkout), str(bare_repo)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            source_url = bare_repo.resolve().as_uri()
            _init_agentteam_profile_for_test(repo, work_root, "update-project")
            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)

            first = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "HEAD",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            missing = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from-git",
                    source_url,
                    "--ref",
                    "missing-ref",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            first_release = json.loads(first.stdout)["release"]
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("git ref not found", missing.stderr)
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], first_release["release_id"])
            self.assertEqual(active["release_root"], first_release["release_root"])

    def test_agentteam_cli_update_from_installs_and_activates_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            existing_run = work_root / "runs" / "existing-run"
            stale_release = work_root / "releases" / "stale-release"
            _init_repo(repo)
            _init_repo(checkout)
            runtime_pkg = checkout / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# fixture runtime\n", encoding="utf-8")
            (checkout / "agentteam").write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture agentteam release"],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            stale_release.mkdir(parents=True)
            (stale_release / "manifest.json").write_text(
                json.dumps(
                    {
                        "release_id": "stale-release",
                        "release_root": str(stale_release),
                        "source_root": str(tmp_path / "old-checkout"),
                    }
                ),
                encoding="utf-8",
            )
            (existing_run / "state").mkdir(parents=True)
            (existing_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "runtime_release_id": "old-release",
                        "runtime_release_root": str(work_root / "releases" / "old-release"),
                    }
                ),
                encoding="utf-8",
            )

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            self.assertEqual(summary["update_status"], "installed")
            self.assertEqual(summary["active_release"]["release_id"], "fixture-release")
            self.assertEqual(summary["latest_installed_release"]["release_id"], "fixture-release")
            self.assertTrue(summary["active_is_latest"])
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["stale-release"])
            release_root = Path(summary["active_release"]["release_root"])
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue((release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime" / "__init__.py").exists())
            self.assertFalse(stale_release.exists())
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], "fixture-release")
            existing_state = json.loads(
                (existing_run / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(existing_state["runtime_release_id"], "old-release")

            text_update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release-text",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_update_completed.returncode, 0, text_update_completed.stderr)
            self.assertIn("update_status: installed\n", text_update_completed.stdout)
            self.assertIn("active_release: fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("latest_installed_release: fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("active_is_latest: true\n", text_update_completed.stdout)
            self.assertIn("  - fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("pruned_releases:\n  - fixture-release\n", text_update_completed.stdout)
            self.assertNotIn("release_root", text_update_completed.stdout)
            self.assertNotIn(str(work_root), text_update_completed.stdout)

    def test_release_prune_keeps_active_and_running_run_release(self):
        from agentteam_runtime.release_manager import prune_releases

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            releases = work_root / "releases"
            for release_id in ["active-release", "running-release", "idle-release"]:
                release_root = releases / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            (releases / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(releases / "active-release"),
                    }
                ),
                encoding="utf-8",
            )
            for run_id, scheduler_status, release_id in [
                ("running-run", "running", "running-release"),
                ("idle-run", "idle", "idle-release"),
            ]:
                state_dir = work_root / "runs" / run_id / "state"
                state_dir.mkdir(parents=True)
                (state_dir / "two_phase_scheduler_state.json").write_text(
                    json.dumps(
                        {
                            "scheduler_status": scheduler_status,
                            "runtime_release_id": release_id,
                            "runtime_release_root": str(releases / release_id),
                        }
                    ),
                    encoding="utf-8",
                )

            result = prune_releases(work_root, keep_latest=1)

            self.assertEqual(result["deleted_release_ids"], ["idle-release"])
            self.assertEqual(result["protected_release_ids"], ["active-release", "running-release"])
            self.assertTrue((releases / "active-release").exists())
            self.assertTrue((releases / "running-release").exists())
            self.assertFalse((releases / "idle-release").exists())

    def test_agentteam_cli_update_prune_deletes_old_terminal_release(self):
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
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for release_id in ["active-release", "old-release"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(work_root / "releases" / "active-release"),
                    }
                ),
                encoding="utf-8",
            )
            idle_run_state = work_root / "runs" / "idle-run" / "state"
            idle_run_state.mkdir(parents=True)
            (idle_run_state / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "runtime_release_id": "old-release",
                        "runtime_release_root": str(work_root / "releases" / "old-release"),
                    }
                ),
                encoding="utf-8",
            )

            prune_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--prune",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(prune_completed.returncode, 0, prune_completed.stderr)
            summary = json.loads(prune_completed.stdout)
            self.assertEqual(summary["update_status"], "pruned")
            self.assertEqual(summary["release_prune"]["deleted_release_ids"], ["old-release"])
            self.assertTrue((work_root / "releases" / "active-release").exists())
            self.assertFalse((work_root / "releases" / "old-release").exists())

    def test_agentteam_cli_start_records_active_runtime_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "active-release"
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
                    "release-record-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )

            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Record active release on run.",
                    "--taskpack-id",
                    "release-record-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            run_state_dir = work_root / "runs" / "release-record-run" / "state"
            state_path = run_state_dir / "two_phase_scheduler_state.json"
            if not state_path.exists():
                state_path = run_state_dir / "scheduler_state.json"
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(state["runtime_release_id"], "active-release")
            self.assertEqual(state["runtime_release_root"], str(release_root))

    def test_agentteam_cli_help_lists_commands_and_command_details(self):
        help_completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", "help"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        stop_completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", "help", "stop"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(help_completed.returncode, 0, help_completed.stderr)
        self.assertIn("AgentTeam commands", help_completed.stdout)
        self.assertIn("init", help_completed.stdout)
        self.assertIn("start", help_completed.stdout)
        self.assertIn("next", help_completed.stdout)
        self.assertIn("status", help_completed.stdout)
        self.assertIn("paths", help_completed.stdout)
        self.assertIn("integrate", help_completed.stdout)
        self.assertIn("notify", help_completed.stdout)
        self.assertIn("watch", help_completed.stdout)
        self.assertIn("stop", help_completed.stdout)
        self.assertIn("taskpack", help_completed.stdout)
        self.assertIn("update", help_completed.stdout)
        self.assertIn("agentteam help <command>", help_completed.stdout)
        self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
        self.assertIn("agentteam stop", stop_completed.stdout)
        self.assertIn("Stop or clean up an existing run", stop_completed.stdout)
        self.assertIn("agentteam stop --project-root <repo>", stop_completed.stdout)
        self.assertIn("--stale", stop_completed.stdout)

    def test_agentteam_cli_continue_runs_existing_frozen_taskpack_without_draft(self):
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
                    "continue-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create frozen taskpack for continue.",
                    "--taskpack-id",
                    "cli-continue",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            shutil.rmtree(work_root / "drafts" / "cli-continue")

            continue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "continue",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "cli-continue",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(continue_completed.returncode, 0, continue_completed.stderr)
            summary = json.loads(continue_completed.stdout)
            self.assertEqual(summary["continue_status"], "continued")
            self.assertEqual(summary["taskpack_id"], "cli-continue")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")

    def test_agentteam_cli_next_creates_followup_taskpack_from_completed_run(self):
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
                    "next-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            first_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Initial optimization pass.",
                    "--taskpack-id",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(first_completed.returncode, 0, first_completed.stderr)

            next_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "next",
                    "--project-root",
                    str(repo),
                    "--from-taskpack",
                    "first-pass",
                    "--goal",
                    "Plan and implement the next optimization step.",
                    "--taskpack-id",
                    "second-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(next_completed.returncode, 0, next_completed.stderr)
            summary = json.loads(next_completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "second-pass")
            self.assertEqual(summary["follow_up"]["source_taskpack_id"], "first-pass")
            self.assertTrue(Path(summary["follow_up"]["source_report_path"]).exists())
            drafted = json.loads((work_root / "drafts" / "second-pass" / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertIn("Follow-up goal:", drafted["goal"])
            self.assertIn("Plan and implement the next optimization step.", drafted["goal"])
            self.assertIn("Previous taskpack context:", drafted["goal"])
            self.assertIn("source_taskpack_id: first-pass", drafted["goal"])
            self.assertIn("final_report.md", drafted["goal"])

    def test_agentteam_cli_next_default_output_is_concise(self):
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
                    "next-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            first_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Initial optimization pass.",
                    "--taskpack-id",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(first_completed.returncode, 0, first_completed.stderr)

            next_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "next",
                    "--project-root",
                    str(repo),
                    "--from-taskpack",
                    "first-pass",
                    "--goal",
                    "Plan and implement the next optimization step.",
                    "--taskpack-id",
                    "second-pass",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(next_completed.returncode, 0, next_completed.stderr)
            self.assertIn("status: completed\n", next_completed.stdout)
            self.assertIn("taskpack_id: second-pass\n", next_completed.stdout)
            self.assertIn("source_taskpack_id: first-pass\n", next_completed.stdout)
            self.assertIn("report:", next_completed.stdout)
            self.assertNotIn('"draft"', next_completed.stdout)
            self.assertLessEqual(len([line for line in next_completed.stdout.splitlines() if line.strip()]), 9)

    def test_repo_root_agentteam_launcher_invokes_cli_help(self):
        launcher = Path(__file__).resolve().parents[4] / "agentteam"

        completed = subprocess.run(
            [str(launcher), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("AgentTeam operator CLI", completed.stdout)

    def test_repo_root_agentteam_launcher_dispatches_active_release_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "fixture-release"
            runtime_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            launcher = Path(__file__).resolve().parents[4] / "agentteam"
            repo.mkdir()
            (repo / ".agentteam").mkdir()
            (repo / ".agentteam" / "profile.json").write_text(
                json.dumps(
                    {
                        "profile_schema_version": "agentteam_profile.v1",
                        "project_key": "launcher-release",
                        "work_root": str(work_root),
                        "author_runtime": "fake",
                        "default_runtime": "fake",
                        "one_shot": True,
                        "max_inflight": 2,
                        "max_attempts": 1,
                        "commit_verified_integration": False,
                        "notification_project": "launcher-release",
                        "feishu": {"enabled": False, "webhook_env": None, "signing_secret_env": None},
                    }
                ),
                encoding="utf-8",
            )
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            (runtime_package / "agentteam.py").write_text(
                "def main(argv=None):\n    print('active release runtime marker')\n    return 0\n",
                encoding="utf-8",
            )
            (work_root / "releases").mkdir(parents=True, exist_ok=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "fixture-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [str(launcher), "status", "--project-root", str(repo)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "active release runtime marker")

    def test_repo_root_agentteam_launcher_start_runs_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            launcher = Path(__file__).resolve().parents[4] / "agentteam"
            env = _test_env()
            env.pop("PYTHONPATH", None)
            _init_repo(repo)

            init_completed = subprocess.run(
                [
                    str(launcher),
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "launcher-start",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)

            completed = subprocess.run(
                [
                    str(launcher),
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Run launcher start without PYTHONPATH.",
                    "--taskpack-id",
                    "launcher-start-fake",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "launcher-start-fake")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")

    def test_agentteam_cli_draft_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            draft_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "draft",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Draft through CLI.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-draft",
                    "--author-runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(draft_completed.returncode, 0, draft_completed.stderr)
            draft_summary = json.loads(draft_completed.stdout)
            self.assertEqual(draft_summary["taskpack_id"], "cli-draft")

            validate_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "validate",
                    str(drafts / "cli-draft"),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(validate_completed.returncode, 0, validate_completed.stderr)
            self.assertEqual(json.loads(validate_completed.stdout)["status"], "accepted")

    def test_agentteam_cli_freeze_after_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)

            draft_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "draft",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Freeze through CLI.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-freeze",
                    "--author-runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(draft_completed.returncode, 0, draft_completed.stderr)

            freeze_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "freeze",
                    str(drafts / "cli-freeze"),
                    "--frozen-root",
                    str(frozen_root),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(freeze_completed.returncode, 0, freeze_completed.stderr)
            freeze_summary = json.loads(freeze_completed.stdout)
            self.assertEqual(freeze_summary["manifest"]["taskpack_id"], "cli-freeze")
            self.assertTrue((frozen_root / "cli-freeze" / "manifest.json").exists())

    def test_agentteam_cli_run_fake_frozen_taskpack_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Run fake frozen taskpack through CLI.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="cli-run-fake",
            )
            taskpack_dir = Path(result["taskpack_dir"])
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "fake"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            agent_pool_path = taskpack_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"]["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = ["generated/"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)

            run_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    frozen["frozen_taskpack_dir"],
                    "--run-root",
                    str(run_root),
                    "--one-shot",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(run_completed.returncode, 0, run_completed.stderr)
            run_summary = json.loads(run_completed.stdout)
            self.assertEqual(run_summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                run_summary["run"]["snapshot"]["tasks"]["TASK-CLI_RUN_FAKE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_run_forwards_child_failure_exit_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            wrapper_run_root = tmp_path / "wrapper-runs"
            direct_run_root = tmp_path / "direct-runs"
            _init_repo(repo)
            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Forward child runtime CLI failure.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="cli-child-failure",
            )
            taskpack_dir = Path(result["taskpack_dir"])
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "fake"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            agent_pool_path = taskpack_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"]["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)

            direct_args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=direct_run_root,
                max_inflight=0,
            )
            direct_completed = subprocess.run(
                ["python3", "-m", "agentteam_runtime.cli", *direct_args],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(direct_completed.returncode, 2, direct_completed.stderr)
            self.assertIn("--max-inflight must be at least 1", direct_completed.stderr)

            wrapper_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    frozen["frozen_taskpack_dir"],
                    "--run-root",
                    str(wrapper_run_root),
                    "--max-inflight",
                    "0",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(wrapper_completed.returncode, direct_completed.returncode)
            self.assertEqual(wrapper_completed.stdout, direct_completed.stdout)
            self.assertEqual(wrapper_completed.stderr, direct_completed.stderr)

    def test_agentteam_cli_run_prelaunch_failure_returns_json_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_taskpack = tmp_path / "missing"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    str(missing_taskpack),
                    "--run-root",
                    str(tmp_path / "runs"),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            error = json.loads(completed.stderr)
            self.assertEqual(error["status"], "error")
            self.assertIn("missing", error["error"])

    def test_agentteam_cli_failure_returns_json_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_taskpack = Path(tmp) / "missing"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "validate",
                    str(missing_taskpack),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            error = json.loads(completed.stderr)
            self.assertEqual(error["status"], "error")
            self.assertIn("missing", error["error"])

    def test_fake_taskpack_author_drafts_safe_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="fake-authored",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["taskpack_id"], "fake-authored")
            self.assertEqual(loaded["backlog"]["items"][0]["required_role"], "implementation_worker")
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_fake_taskpack_author_marks_optimization_goals_code_facing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="阅读这个比赛代码仓库并检查能否优化现有工作。",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="optimize-competition",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            task = loaded["backlog"]["items"][0]
            self.assertEqual(loaded["taskpack"]["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("optimization_candidate_matrix", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_fake_taskpack_author_draft_can_be_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="fake-freezable",
            )

            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            self.assertEqual(frozen["manifest"]["taskpack_id"], "fake-freezable")
            self.assertTrue((Path(frozen["frozen_taskpack_dir"]) / "manifest.json").exists())

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

    def test_canonicalize_codex_taskpack_restores_optimization_goal_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository latency.",
                draft_root=drafts,
                taskpack_id="canonicalize-optimization",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["goal_kind"] = "audit"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["goal_kind"], "optimization")
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

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
            backlog["items"][0]["objective"] = "Audit repository completeness and fix concrete in-repo gaps."
            backlog["items"][0]["goal_alignment"] = "Check whether the repository is ready to submit."
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("optimization task must preserve optimization intent", str(raised.exception))

    def test_taskpack_author_uses_unique_implicit_id_when_default_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            first = draft_taskpack_from_goal(
                project_root=repo,
                goal="优化现有比赛代码",
                draft_root=drafts,
                author_runtime="fake",
            )
            second = draft_taskpack_from_goal(
                project_root=repo,
                goal="优化现有比赛代码",
                draft_root=drafts,
                author_runtime="fake",
            )

            self.assertEqual(first["taskpack_id"], "taskpack")
            self.assertEqual(second["taskpack_id"], "taskpack-2")
            self.assertTrue((drafts / "taskpack").exists())
            self.assertTrue((drafts / "taskpack-2").exists())

    def test_taskpack_author_rejects_explicit_existing_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            draft_taskpack_from_goal(
                project_root=repo,
                goal="Create explicit taskpack.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="explicit-repeat",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Create explicit taskpack again.",
                    draft_root=drafts,
                    author_runtime="fake",
                    taskpack_id="explicit-repeat",
                )

            self.assertIn("already exists", str(raised.exception))

    def test_taskpack_author_rejects_unsupported_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            with self.assertRaises(TaskpackValidationError):
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="human",
                    taskpack_id="unsupported-author",
                )

    def test_codex_taskpack_author_default_command_allows_non_git_draft_root(self):
        self.assertEqual(_command_list(None), ["codex", "exec", "--skip-git-repo-check"])

    def test_codex_taskpack_author_canonicalizes_common_schema_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-aliases"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-aliases",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Improve existing project.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_id": "runtime-optimization-audit-001",
                                "title": "Audit and optimize runtime path",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["README.md"],
                                "write_scope": ["src/runtime.py"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps({"command": ["python3", "-m", "unittest", "discover"]}),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            agent_pool = json.loads((taskpack_dir / "agent_pool.json").read_text(encoding="utf-8"))
            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            self.assertEqual(agent_pool["scheduler_agent_id"], "agent-scheduler")
            self.assertEqual(
                agent_pool["agents"][0]["inbox_path"],
                "mailboxes/implementation-worker-1/inbox.jsonl",
            )
            self.assertEqual(
                agent_pool["agents"][0]["outbox_path"],
                "mailboxes/implementation-worker-1/outbox.jsonl",
            )
            self.assertEqual(backlog["items"][0]["task_id"], "runtime-optimization-audit-001")
            self.assertEqual(backlog["items"][0]["objective"], "Audit and optimize runtime path")
            self.assertEqual(backlog["items"][0]["backlog_status"], "ready")
            self.assertEqual(backlog["items"][0]["blockers"], [])

    def test_codex_taskpack_author_canonicalizes_optimization_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-optimization-contract"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-optimization-contract",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "优化现有比赛代码，寻找可以验证的代码改进。",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_id": "optimize-code-001",
                                "title": "Find and implement one safe optimization",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["."],
                                "write_scope": ["src/"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps({"command": ["python3", "-m", "unittest", "discover"]}),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            task = backlog["items"][0]
            self.assertEqual(taskpack["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_uses_project_venv_for_python_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "venv-command"
            _init_repo(repo)
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "venv-command",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Use the project test environment.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/implementation-worker-1/inbox.jsonl",
                                "outbox_path": "mailboxes/implementation-worker-1/outbox.jsonl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-VENV-001",
                                "objective": "Use project venv.",
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["gesture_recognition/tests"],
                                "write_scope": ["gesture_recognition/"],
                                "blockers": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps(
                    {
                        "command": [
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "gesture_recognition/tests",
                            "-v",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                [
                    str(venv_python.resolve()),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "gesture_recognition/tests",
                    "-v",
                ],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_preserves_project_venv_symlink_for_python_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "venv-symlink-command"
            _init_repo(repo)
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(Path(sys.executable))
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "venv-symlink-command",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Use the project venv symlink.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/implementation-worker-1/inbox.jsonl",
                                "outbox_path": "mailboxes/implementation-worker-1/outbox.jsonl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-VENV-SYMLINK-001",
                                "objective": "Use project venv symlink.",
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["gesture_recognition/tests"],
                                "write_scope": ["gesture_recognition/"],
                                "blockers": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps(
                    {
                        "command": [
                            "/usr/bin/python3.12",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "gesture_recognition/tests",
                            "-v",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                [
                    str(venv_python),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "gesture_recognition/tests",
                    "-v",
                ],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_normalizes_system_python_verification_without_project_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Normalize system Python verification.",
                draft_root=drafts,
                taskpack_id="system-python-command",
                read_scope=["README.md"],
                write_scope=["README.md"],
                verification_command=["/usr/bin/python3.12", "-m", "unittest", "discover"],
            )
            taskpack_dir = Path(result["taskpack_dir"])

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_rejects_dirty_repo_before_running_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_codex_author.py"
            marker = tmp_path / "codex-ran.marker"
            _init_repo(repo)
            (repo / "untracked.txt").write_text("preexisting untracked file\n", encoding="utf-8")
            fake_codex.write_text(
                "\n".join(
                    [
                        "import pathlib",
                        f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="dirty-codex-author",
                    codex_command=["python3", str(fake_codex)],
                    codex_timeout_seconds=5,
                )

            self.assertIn("clean", str(raised.exception))
            self.assertFalse(marker.exists())

    def test_codex_taskpack_author_reports_target_repo_change_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_timeout_author.py"
            changed_file = "codex-timeout-side-effect.txt"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import pathlib",
                        "import sys",
                        "import time",
                        "repo = pathlib.Path(sys.argv[1])",
                        f"(repo / {changed_file!r}).write_text('changed\\n', encoding='utf-8')",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="timeout-side-effect",
                    codex_command=["python3", str(fake_codex), str(repo)],
                    codex_timeout_seconds=1,
                )

            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIn("modified the target repository", str(raised.exception))
            self.assertIn(changed_file, status.stdout)

    def test_codex_taskpack_author_rejects_committed_target_repo_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            taskpack_dir = drafts / "committed-side-effect"
            fake_codex = tmp_path / "fake_committing_author.py"
            changed_file = "codex-committed-side-effect.txt"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import subprocess",
                        "import sys",
                        "repo = pathlib.Path(sys.argv[1]).resolve()",
                        "taskpack_dir = pathlib.Path(sys.argv[2]).resolve()",
                        f"changed_file = {changed_file!r}",
                        "(repo / changed_file).write_text('changed\\n', encoding='utf-8')",
                        "subprocess.run(['git', 'add', changed_file], cwd=repo, check=True)",
                        (
                            "subprocess.run(['git', 'commit', '-m', 'codex side effect'], "
                            "cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)"
                        ),
                        "taskpack_id = taskpack_dir.name",
                        "task_id = 'TASK-COMMITTED-SIDE-EFFECT-001'",
                        "taskpack = {",
                        "    'taskpack_schema_version': 'taskpack.v1',",
                        "    'taskpack_id': taskpack_id,",
                        "    'status': 'draft',",
                        "    'project_root': str(repo),",
                        "    'goal': 'Improve fixture behavior.',",
                        "    'runtime': {'default_backend': 'codex'},",
                        (
                            "    'policy': {'allow_merge': False, "
                            "'merge_requires_verified_integration': True},"
                        ),
                        (
                            "    'files': {'agent_pool': 'agent_pool.json', "
                            "'backlog': 'backlog.json', 'verification': 'verification.json'},"
                        ),
                        "}",
                        "agent_pool = {",
                        "    'scheduler_agent_id': 'agent-scheduler',",
                        "    'role_runtime_profiles': {'implementation_worker': {'adapter': 'codex'}},",
                        "    'agents': [{",
                        "        'agent_id': 'agent-implementation-worker-1',",
                        "        'role': 'implementation_worker',",
                        "        'status': 'idle',",
                        "        'inbox_path': 'mailboxes/agent-implementation-worker-1/inbox.jsonl',",
                        "    }],",
                        "}",
                        "backlog = {'backlog_id': 'BL-committed-side-effect', 'items': [{",
                        "    'task_id': task_id,",
                        "    'milestone_id': 'TASKPACK-M0',",
                        "    'objective': 'Improve fixture behavior.',",
                        "    'backlog_status': 'ready',",
                        "    'risk_target': 'L1',",
                        "    'depends_on': [],",
                        "    'read_scope': ['.'],",
                        "    'write_scope': ['src/'],",
                        "    'required_role': 'implementation_worker',",
                        "    'blockers': [],",
                        "}]}",
                        (
                            "verification = {'verification_schema_version': "
                            "'taskpack_verification.v1', 'command': ['python3', '-m', "
                            "'unittest', 'discover'], 'success_criteria': ['tests pass']}"
                        ),
                        "for name, payload in [",
                        "    ('taskpack.yaml', taskpack),",
                        "    ('agent_pool.json', agent_pool),",
                        "    ('backlog.json', backlog),",
                        "    ('verification.json', verification),",
                        "]:",
                        (
                            "    (taskpack_dir / name).write_text(json.dumps(payload), "
                            "encoding='utf-8')"
                        ),
                        "(taskpack_dir / 'README.md').write_text('# committed-side-effect\\n', encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="committed-side-effect",
                    codex_command=["python3", str(fake_codex), str(repo), str(taskpack_dir)],
                    codex_timeout_seconds=5,
                )

            self.assertIn("modified the target repository", str(raised.exception))

    def test_codex_taskpack_author_records_timeout_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_timeout_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import time",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError):
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="author-state-timeout",
                    codex_command=["python3", str(fake_codex)],
                    codex_timeout_seconds=1,
                )

            state_path = drafts / ".author-state-timeout-author" / "author_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["author_status"], "timed_out")
            self.assertEqual(state["taskpack_id"], "author-state-timeout")
            self.assertEqual(state["timeout_seconds"], 1)
            self.assertTrue(state["pid"])
            self.assertTrue(Path(state["prompt_path"]).exists())
            self.assertTrue(Path(state["result_path"]).exists())

    def test_project_authoring_summary_reports_running_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            author_dir = work_root / "drafts" / ".running-author"
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "running",
                    "pid": os.getpid(),
                    "started_at": "2026-06-10T00:00:00Z",
                    "updated_at": "2026-06-10T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_project_authoring_summary(profile)

            self.assertEqual(summary["active_count"], 1)
            self.assertEqual(summary["latest"]["taskpack_id"], "running")
            self.assertEqual(summary["latest"]["liveness_status"], "running-alive")

    def test_run_status_summary_reports_overall_authoring_when_followup_author_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = work_root / "runs" / "previous-run"
            author_dir = work_root / "drafts" / ".follow-up-author"
            _write_completed_operator_run(run_dir)
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "follow-up",
                    "pid": os.getpid(),
                    "started_at": "2026-06-11T00:00:00Z",
                    "updated_at": "2026-06-11T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_run_status_summary(profile, run_dir)

            self.assertEqual(summary["status"], "idle")
            self.assertEqual(summary["run_status"], "idle")
            self.assertEqual(summary["overall_status"], "authoring")
            self.assertEqual(summary["active_phase"], "authoring")
            self.assertEqual(summary["active_authoring"]["taskpack_id"], "follow-up")

    def test_status_text_separates_overall_and_run_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = work_root / "runs" / "previous-run"
            author_dir = work_root / "drafts" / ".follow-up-author"
            _write_completed_operator_run(run_dir)
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "follow-up",
                    "pid": os.getpid(),
                    "started_at": "2026-06-11T00:00:00Z",
                    "updated_at": "2026-06-11T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}
            summary = _build_run_status_summary(profile, run_dir)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                _write_status_text(summary)

            output = stdout.getvalue()
            self.assertIn("overall_status: authoring", output)
            self.assertIn("run_status: idle", output)
            self.assertIn("active_phase: authoring", output)
            self.assertIn("active_authoring: follow-up", output)
            self.assertNotIn("\nstatus: idle\n", output)

    def test_stop_authoring_terminates_recorded_author_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            author_dir = work_root / "drafts" / ".sleep-author"
            author_dir.mkdir(parents=True)
            process = subprocess.Popen(
                ["python3", "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _write_json(
                    author_dir / "author_state.json",
                    {
                        "author_status": "running",
                        "taskpack_id": "sleep",
                        "pid": process.pid,
                        "started_at": "2026-06-10T00:00:00Z",
                        "updated_at": "2026-06-10T00:00:01Z",
                    },
                )
                profile = {"project_key": "fixture", "work_root": str(work_root)}

                summary = _stop_authoring(profile, grace_seconds=1, force=True, operator="tester")

                process.wait(timeout=5)
                state = json.loads((author_dir / "author_state.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["stop_status"], "stopped_authoring")
                self.assertEqual(summary["taskpack_id"], "sleep")
                self.assertEqual(state["author_status"], "stopped")
                self.assertEqual(state["stopped_by"], "tester")
                self.assertIn(state["stop_signal"], {"SIGTERM", "SIGKILL"})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_run_paths_for_frozen_taskpack_accepts_concrete_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            runs_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args.",
                draft_root=drafts,
                taskpack_id="runtime-paths",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            paths = _run_paths_for_frozen_taskpack(
                frozen["frozen_taskpack_dir"],
                runs_root / "runtime-paths",
            )

            self.assertEqual(paths["taskpack_id"], "runtime-paths")
            self.assertEqual(paths["run_root"], runs_root.resolve())
            self.assertEqual(paths["run_dir"], (runs_root / "runtime-paths").resolve())
            self.assertTrue(paths["normalized_from_concrete_run_dir"])

    def test_canonical_run_dir_resolves_nested_low_level_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outer = tmp_path / "runs" / "taskpack-5"
            nested = outer / "taskpack-5"
            nested.mkdir(parents=True)
            (nested / "events.jsonl").write_text("", encoding="utf-8")

            self.assertEqual(_canonical_run_dir(outer), nested.resolve())

    def test_handle_run_prints_compact_summary_and_normalizes_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            runs_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement fixture output.",
                draft_root=drafts,
                taskpack_id="compact-run",
                write_scope=["generated/"],
                verification_command=["python3", "-c", "pass"],
            )
            _set_taskpack_runtime_backend(Path(result["taskpack_dir"]), "fake")
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = _handle_run(
                    SimpleNamespace(
                        frozen_taskpack_dir=frozen["frozen_taskpack_dir"],
                        run_root=str(runs_root / "compact-run"),
                        one_shot=False,
                        max_inflight=1,
                        max_attempts=1,
                        commit_verified_integration=False,
                        notification_project="fixture",
                        feishu_webhook_env=None,
                        feishu_signing_secret_env=None,
                        json=False,
                    )
                )

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("status: completed", output)
            self.assertIn("taskpack_id: compact-run", output)
            self.assertIn("report:", output)
            self.assertIn(f"run_dir: {runs_root / 'compact-run'}", output)
            self.assertNotIn('"snapshot"', output)
            self.assertTrue((runs_root / "compact-run" / "events.jsonl").exists())
            self.assertFalse((runs_root / "compact-run" / "compact-run").exists())

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
            self.assertEqual(
                loaded["verification"]["performance"]["command"],
                ["python3", "tools/bench.py", "--json"],
            )
            self.assertEqual(loaded["verification"]["performance"]["metrics"], ["accuracy", "latency_ms"])

    def test_draft_taskpack_files_writes_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Improve fixture behavior without broad writes.",
                draft_root=drafts,
                taskpack_id="fixture-taskpack",
                write_scope=["src/"],
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            taskpack_dir = Path(result["taskpack_dir"])
            self.assertEqual(taskpack_dir.name, "fixture-taskpack")
            self.assertTrue((taskpack_dir / "taskpack.yaml").exists())
            self.assertTrue((taskpack_dir / "agent_pool.json").exists())
            self.assertTrue((taskpack_dir / "backlog.json").exists())
            self.assertTrue((taskpack_dir / "verification.json").exists())
            self.assertTrue((taskpack_dir / "README.md").exists())

            loaded = load_taskpack(taskpack_dir)
            self.assertEqual(loaded["taskpack"]["taskpack_schema_version"], "taskpack.v1")
            self.assertEqual(loaded["taskpack"]["taskpack_id"], "fixture-taskpack")
            self.assertEqual(loaded["taskpack"]["status"], "draft")
            self.assertEqual(loaded["taskpack"]["semantic_contract_version"], "task_semantics.v1")
            self.assertEqual(loaded["taskpack"]["project_root"], str(repo.resolve()))
            self.assertEqual(loaded["taskpack"]["original_goal"], "Improve fixture behavior without broad writes.")
            self.assertIn("goal_alignment", loaded["backlog"]["items"][0])
            self.assertIn("required_deliverables", loaded["backlog"]["items"][0])
            self.assertIn("verification_summary", loaded["backlog"]["items"][0]["required_deliverables"])
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            self.assertEqual(loaded["backlog"]["items"][0]["write_scope"], ["src/"])

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

    def test_draft_taskpack_files_rejects_unsafe_taskpack_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            cases = [
                ("../escape", tmp_path / "escape"),
                (str(tmp_path / "absolute"), tmp_path / "absolute"),
            ]
            for taskpack_id, escaped_path in cases:
                with self.subTest(taskpack_id=taskpack_id):
                    with self.assertRaises(TaskpackValidationError):
                        draft_taskpack_files(
                            project_root=repo,
                            goal="Reject unsafe taskpack IDs.",
                            draft_root=drafts,
                            taskpack_id=taskpack_id,
                        )
                    self.assertFalse(escaped_path.exists())

    def test_draft_taskpack_files_rejects_invalid_string_sequences(self):
        cases = [
            {"read_scope": "."},
            {"write_scope": "src/"},
            {"verification_command": "python3 -m unittest"},
            {"write_scope": ["src/", 123]},
        ]

        for index, kwargs in enumerate(cases):
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)

                    with self.assertRaises(TaskpackValidationError):
                        draft_taskpack_files(
                            project_root=repo,
                            goal="Reject invalid sequence inputs.",
                            draft_root=drafts,
                            taskpack_id=f"sequence-{index}",
                            **kwargs,
                        )

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

    def test_validate_taskpack_rejects_broad_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject broad writes.",
                draft_root=drafts,
                taskpack_id="bad-write-scope",
                write_scope=["."],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope must not include repository root", str(raised.exception))

    def test_validate_taskpack_rejects_normalized_root_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject normalized root write scope.",
                draft_root=drafts,
                taskpack_id="normalized-root-write-scope",
                write_scope=["./."],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_rejects_parent_relative_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject parent-relative write scope.",
                draft_root=drafts,
                taskpack_id="parent-write-scope",
                write_scope=["../outside"],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_rejects_root_wide_glob_write_scope(self):
        cases = ["./*", "**/*", "./**", "./**/*"]
        for index, write_scope in enumerate(cases):
            with self.subTest(write_scope=write_scope):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject broad glob write scope.",
                        draft_root=drafts,
                        taskpack_id=f"root-glob-write-scope-{index}",
                        write_scope=[write_scope],
                    )

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_rejects_root_prefix_wildcard_write_scope(self):
        cases = ["*.py", "*/*.py", "*/**/*"]
        for index, write_scope in enumerate(cases):
            with self.subTest(write_scope=write_scope):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject root-prefix wildcard write scope.",
                        draft_root=drafts,
                        taskpack_id=f"root-prefix-wildcard-{index}",
                        write_scope=[write_scope],
                    )

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("write_scope", str(raised.exception))

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

    def test_validate_taskpack_rejects_missing_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing taskpack id.",
                draft_root=drafts,
                taskpack_id="missing-taskpack-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["taskpack_id"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack_id", str(raised.exception))

    def test_validate_taskpack_rejects_unsafe_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unsafe taskpack id.",
                draft_root=drafts,
                taskpack_id="unsafe-taskpack-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["taskpack_id"] = "../escaped"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack_id", str(raised.exception))

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

    def test_validate_taskpack_rejects_missing_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing project root.",
                draft_root=drafts,
                taskpack_id="missing-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["project_root"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("project_root", str(raised.exception))

    def test_validate_taskpack_rejects_file_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject file project root.",
                draft_root=drafts,
                taskpack_id="file-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["project_root"] = str(repo / "README.md")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("project_root", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed goal.",
                draft_root=drafts,
                taskpack_id="malformed-goal",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["goal"] = ["not", "a", "string"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("goal", str(raised.exception))

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

    def test_validate_taskpack_rejects_non_list_backlog_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed backlog items.",
                draft_root=drafts,
                taskpack_id="malformed-backlog-items",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"] = {"task_id": "TASK"}
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("backlog.items", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task id.",
                draft_root=drafts,
                taskpack_id="malformed-task-id",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["task_id"] = ["TASK"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("task_id", str(raised.exception))

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

    def test_validate_taskpack_rejects_invalid_depends_on_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed dependencies.",
                draft_root=drafts,
                taskpack_id="malformed-depends-on",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["depends_on"] = None
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("depends_on", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_dependency_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed dependency entries.",
                draft_root=drafts,
                taskpack_id="malformed-dependency-entry",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["depends_on"] = [123]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("depends_on", str(raised.exception))

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

    def test_validate_taskpack_rejects_invalid_write_scope_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject invalid write scope.",
                draft_root=drafts,
                taskpack_id="invalid-write-scope",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = None
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope must be a non-empty list", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_write_scope_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed write scope entries.",
                draft_root=drafts,
                taskpack_id="malformed-write-scope",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = [123]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

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

    def test_build_taskpack_runtime_args_uses_frozen_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args.",
                draft_root=drafts,
                taskpack_id="runtime-args",
                write_scope=["src/"],
                verification_command=["python3", "-m", "unittest", "discover"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                daemon=True,
                max_inflight=2,
                commit_verified_integration=False,
            )

            self.assertEqual(
                args[0:2],
                ["--agent-pool", str(Path(frozen["frozen_taskpack_dir"]) / "agent_pool.json")],
            )
            self.assertEqual(_arg_value(args, "--runtime"), "codex")
            self.assertEqual(
                json.loads(_arg_value(args, "--integration-verification-command-json")),
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertIn("--daemon-run-until-idle", args)
            self.assertIn("--daemon-two-phase-worker-pool", args)
            self.assertEqual(_arg_value(args, "--max-steps"), "45000")
            self.assertEqual(_arg_value(args, "--codex-timeout-seconds"), "1800")
            self.assertEqual(_arg_value(args, "--lease-timeout-seconds"), "1860")
            self.assertIn("--integrate-accepted-patch", args)
            self.assertNotIn("--commit-verified-integration", args)
            self.assertTrue((run_root / "runtime-args").exists())

    def test_build_taskpack_runtime_args_supports_one_shot_and_commit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build one-shot runtime args.",
                draft_root=drafts,
                taskpack_id="one-shot-runtime-args",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                daemon=False,
                commit_verified_integration=True,
            )

            self.assertIn("--run-until-idle", args)
            self.assertNotIn("--daemon-run-until-idle", args)
            self.assertNotIn("--daemon-two-phase-worker-pool", args)
            self.assertIn("--commit-verified-integration", args)
            self.assertEqual(_arg_value(args, "--runtime"), "codex")

    def test_build_taskpack_runtime_args_rejects_draft_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject draft runtime launch.",
                draft_root=drafts,
                taskpack_id="draft-runtime-args",
                write_scope=["src/"],
            )

            with self.assertRaises(TaskpackValidationError):
                build_taskpack_runtime_args(result["taskpack_dir"], run_root=run_root)

            self.assertFalse((run_root / "draft-runtime-args").exists())

    def test_build_taskpack_runtime_args_honors_mapped_companion_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Use mapped runtime companion files.",
                draft_root=drafts,
                taskpack_id="mapped-runtime-args",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            nested_dir = taskpack_dir / "nested"
            nested_dir.mkdir()
            (taskpack_dir / "agent_pool.json").replace(nested_dir / "agent_pool.json")
            (taskpack_dir / "backlog.json").replace(nested_dir / "backlog.json")
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["files"]["agent_pool"] = "nested/agent_pool.json"
            taskpack["files"]["backlog"] = "nested/backlog.json"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])

            args = build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            self.assertEqual(_arg_value(args, "--agent-pool"), str(frozen_dir / "nested" / "agent_pool.json"))
            self.assertEqual(_arg_value(args, "--backlog"), str(frozen_dir / "nested" / "backlog.json"))

    def test_build_taskpack_runtime_args_defaults_missing_files_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Use default runtime companion files.",
                draft_root=drafts,
                taskpack_id="default-runtime-files",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["files"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])

            args = build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            self.assertEqual(_arg_value(args, "--agent-pool"), str(frozen_dir / "agent_pool.json"))
            self.assertEqual(_arg_value(args, "--backlog"), str(frozen_dir / "backlog.json"))
            self.assertTrue((run_root / "default-runtime-files").exists())

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

    def test_build_taskpack_runtime_args_rejects_invalid_runtime_without_run_dir(self):
        cases = [
            ("non-object-runtime", []),
            ("missing-runtime-backend", {}),
            ("unknown-runtime-backend", {"default_backend": "unknown"}),
        ]
        for taskpack_id, runtime in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    run_root = tmp_path / "runs"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid runtime metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
                    taskpack_path = Path(frozen["frozen_taskpack_dir"]) / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["runtime"] = runtime
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

                    self.assertIn("runtime", str(raised.exception))
                    self.assertFalse((run_root / taskpack_id).exists())

    def test_build_taskpack_runtime_args_rejects_shell_backend_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject shell runtime launch.",
                draft_root=drafts,
                taskpack_id="shell-runtime-backend",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            taskpack_path = Path(frozen["frozen_taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "shell"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertIn("runtime", str(raised.exception))
            self.assertFalse((run_root / "shell-runtime-backend").exists())

    def test_build_taskpack_runtime_args_rejects_tampered_runtime_profile_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject tampered frozen runtime profile.",
                draft_root=drafts,
                taskpack_id="tampered-runtime-profile",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            agent_pool_path = frozen_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"] = {
                "adapter": "shell",
                "command": ["bash", "-lc", "echo unsafe"],
            }
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            message = str(raised.exception)
            self.assertTrue("adapter" in message or "command" in message)
            self.assertFalse((run_root / "tampered-runtime-profile").exists())

    def test_build_taskpack_runtime_args_rejects_invalid_launch_metadata_without_run_dir(self):
        cases = [
            ("missing-project-root", "taskpack.yaml", lambda value: value.pop("project_root")),
            ("missing-verification-command", "verification.json", lambda value: value.pop("command")),
        ]
        for taskpack_id, artifact_name, mutate in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    run_root = tmp_path / "runs"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid launch metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
                    artifact_path = Path(frozen["frozen_taskpack_dir"]) / artifact_name
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    mutate(artifact)
                    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError):
                        build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

                    self.assertFalse((run_root / taskpack_id).exists())

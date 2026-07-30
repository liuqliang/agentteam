import json
import hashlib
import copy
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import io
import runpy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentteam_runtime import (
    TaskpackValidationError,
    build_taskpack_runtime_args,
    draft_deterministic_taskpack_skeleton,
    draft_taskpack_files,
    draft_taskpack_from_goal,
    freeze_taskpack as _freeze_taskpack_impl,
    load_taskpack,
    validate_taskpack,
)
from agentteam_runtime.agentteam import (
    _build_project_authoring_summary,
    _build_run_status_summary,
    _canonical_run_dir,
    _handle_taskpack_materialize,
    _handle_taskpack_new,
    _handle_run,
    _run_paths_for_frozen_taskpack,
    _pursue_next_goal,
    _pursue_next_integration_base_ref,
    _pursue_stop_reason,
    _publish_author_lifecycle_bootstrap,
    _set_taskpack_runtime_backend,
    _submit_args_from_profile,
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
from agentteam_runtime.goal_memory import build_goal_memory, render_goal_memory_prompt_context
from agentteam_runtime.notifications import FeishuWebhookNotifier, _permission_request_text
from agentteam_runtime.operator_report import (
    aggregate_model_invocation_usage,
    compact_model_invocation_usage_lines,
    concise_report_lines,
)
from agentteam_runtime.profile import build_project_profile, write_project_profile
from agentteam_runtime.release_manager import (
    AgentTeamReleaseError,
    adopt_legacy_implementation_run,
    publish_acceptance_run_identity,
    publish_implementation_run,
    prune_releases,
    scan_run_identities,
    select_latest_implementation_run,
    selected_release_identity,
    validate_run_binding,
)
import agentteam_runtime.agentteam as agentteam_module
import agentteam_runtime.experiment_gates as experiment_gates_module
import agentteam_runtime.projection_db as projection_db
from agentteam_runtime.projection_db import (
    check_project_projection_db,
    rebuild_project_projection_db,
)
import agentteam_runtime.taskpack as taskpack_module
from agentteam_runtime.taskpack_author import REQUIRED_TASKPACK_FILES
from agentteam_runtime.taskpack_author import _apply_verification_profile_to_taskpack
from agentteam_runtime.taskpack_author import _command_list
from agentteam_runtime.taskpack_author import _canonicalize_codex_taskpack_files
from agentteam_runtime.taskpack_author import (
    _author_model_invocation_context,
    _run_codex_author_command,
    recover_open_author_invocations,
)
from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    InvocationLifecycle,
    ModelInvocationIntegrityError,
    ProviderExecution,
    import_author_lifecycle_bootstrap,
    import_registered_controller_lifecycles,
    replay_model_invocation_events,
)
from agentteam_runtime.taskpack_author import _author_prompt
from agentteam_runtime.taskpack_author import _run_codex_author_command
from agentteam_runtime.taskpack_author import _write_author_template_bundle


def freeze_taskpack(
    taskpack_dir,
    frozen_root,
    *,
    expected_authoring_mode=None,
):
    if expected_authoring_mode is None:
        taskpack = load_taskpack(taskpack_dir)["taskpack"]
        expected_authoring_mode = (
            taskpack.get("authoring_mode") or "legacy_direct"
        )
    return _freeze_taskpack_impl(
        taskpack_dir,
        frozen_root,
        expected_authoring_mode=expected_authoring_mode,
    )


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


def _implementation_item(backlog):
    items = backlog["items"] if isinstance(backlog, dict) else backlog
    for item in items:
        if item.get("required_role") == "implementation_worker":
            return item
    raise AssertionError("implementation_worker backlog item not found")


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
    schemas = checkout / "experiments" / "native_agentteam_runtime" / "schemas"
    runtime_pkg.mkdir(parents=True, exist_ok=True)
    schemas.mkdir(parents=True, exist_ok=True)
    (runtime_pkg / "__init__.py").write_text(f"# {marker} runtime\n", encoding="utf-8")
    _write_json(schemas / "taskpack_blueprint.schema.json", {"type": "object"})
    _write_json(schemas / "p0_experiment_readiness.schema.json", {"type": "object"})
    _write_json(schemas / "experiment_manifest.schema.json", {"type": "object"})
    _write_json(
        runtime_pkg / "data" / "p0_experiment_readiness.v1.json",
        {"schema_version": "p0_experiment_readiness.v1"},
    )
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


def _pre04_release_fixture(work_root, release_id, source_commit=None, runtime_source=None):
    source_commit = source_commit or ("1" * 40)
    release_root = work_root / "releases" / release_id
    runtime_root = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"
    if runtime_source:
        shutil.copytree(runtime_source / "agentteam_runtime", runtime_root / "agentteam_runtime")
    else:
        package = runtime_root / "agentteam_runtime"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "agentteam.py").write_text("def main(argv=None): return 0\n", encoding="utf-8")
    manifest = {
        "manifest_schema_version": "agentteam_release_manifest.v2",
        "release_id": release_id,
        "release_root": str(release_root),
        "runtime_root": str(runtime_root),
        "source_commit": source_commit,
        "git_object_format": "sha1",
        "installed_at": f"2026-07-25T00:00:0{release_id[-1:] if release_id[-1:].isdigit() else '0'}Z",
    }
    _write_json(release_root / "manifest.json", manifest)
    _write_json(work_root / "releases" / "refs" / f"{release_id}.json", manifest)
    return selected_release_identity(work_root, release_id)


def _blueprint_fixture(repo, task_count=3):
    blueprint_relative = "plans/example.blueprint.json"
    source_plan_relative = "plans/example.md"
    review_schema_relative = "schemas/review.schema.json"
    approval_relative = "reviews/approval.json"
    (repo / source_plan_relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / source_plan_relative).write_text("# Example plan\n", encoding="utf-8")
    _write_json(
        repo / review_schema_relative,
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
    )
    tasks = []
    for index in range(task_count):
        task_id = f"T-{index + 1}"
        tasks.append(
            {
                "task_id": task_id,
                "objective": f"Implement task {index + 1}.",
                "goal_alignment": f"Task {index + 1} advances the fixture.",
                "work_type": "code_implementation",
                "required_role": "implementation_worker",
                "backlog_status": "ready",
                "risk_target": "L1",
                "depends_on": [] if index == 0 else [f"T-{index}"],
                "blockers": [],
                "read_scope": ["README.md"],
                "write_scope": [f"src/task_{index + 1}.py"],
                "required_deliverables": [f"task_{index + 1}_change"],
                "acceptance_criteria": [f"Task {index + 1} passes."],
                "stop_condition": f"Stop if task {index + 1} cannot be verified.",
                "input_artifacts": [blueprint_relative],
                "evidence_paths": ["tests"],
            }
        )
    blueprint = {
        "schema_version": "agentteam_taskpack_blueprint.v1",
        "blueprint_id": "example-blueprint",
        "source_plan": source_plan_relative,
        "taskpack": {
            "taskpack_id": "example-blueprint",
            "goal_kind": "implementation",
            "milestone": "fixture",
            "goal": "Implement the complete fixture blueprint.",
            "overall_risk": "L1",
            "integration_policy": "verified integration",
        },
        "approval": {
            "record_path": approval_relative,
            "schema_path": review_schema_relative,
            "required_decision": "approved",
            "git_object_format_required": True,
            "runtime_release_binding_required": False,
            "digest_bindings": ["source_plan", "blueprint", "review_schema"],
        },
        "agents": [
            {
                "agent_id": "agent-implementation-worker-1",
                "role": "implementation_worker",
                "runtime_profile": {"adapter": "codex"},
            }
        ],
        "verification": {
            "command": ["python3", "-m", "unittest", "discover"],
        },
        "policy": {
            "allow_merge": False,
            "merge_requires_verified_integration": True,
            "operator_review_required": True,
        },
        "tasks": tasks,
        "post_backlog_gates": [
            {
                "gate_id": "FINAL",
                "depends_on": [tasks[-1]["task_id"]],
                "executor": "deterministic_controller",
                "evidence_artifact": "acceptance/final.json",
                "evidence_schema": "src/final.schema.json",
                "required_status_field": "status",
                "required_status_value": "passed",
            }
        ],
    }
    tasks[-1]["write_scope"].append("src/final.schema.json")
    _write_json(repo / blueprint_relative, blueprint)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add blueprint fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _write_blueprint_approval(repo, blueprint)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "approve blueprint fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return blueprint_relative, blueprint


def _write_blueprint_approval(repo, blueprint, decision="approved", escalations=None):
    approval = blueprint["approval"]
    blueprint_path = repo / "plans" / "example.blueprint.json"
    source_plan_path = repo / blueprint["source_plan"]
    review_schema_path = repo / approval["schema_path"]
    record = {
        "decision": decision,
        "remaining_escalations": list(escalations or []),
        "git_object_format": subprocess.run(
            ["git", "rev-parse", "--show-object-format"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip(),
        "preflight_release_id": "fixture-release",
        "preflight_release_source_commit": _git_head(repo),
        "plan_sha256": hashlib.sha256(source_plan_path.read_bytes()).hexdigest(),
        "blueprint_sha256": hashlib.sha256(blueprint_path.read_bytes()).hexdigest(),
        "review_schema_sha256": hashlib.sha256(review_schema_path.read_bytes()).hexdigest(),
    }
    _write_json(repo / approval["record_path"], record)
    return record


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


class _InvocationProbeFakeHost:
    boot_id = "12345678-1234-4234-8234-123456789abc"
    enclosing_invocation_id = "1" * 32
    unit_invocation_id = "2" * 32
    enclosing_cgroup = "/user.slice/user-1000.slice/user@1000.service"

    def __init__(self, scenario="success"):
        self.scenario = scenario
        self.now = 0.0
        self.unit_name = None
        self.unit_started = False
        self.unit_stopped = False
        self.stop_attempted = False
        self.released = False
        self.state_removed = False
        self.pidfd_closed = False
        self.enclosing_show_count = 0
        self.manager_stat_count = 0
        self.helper_stat_count = 0
        self.loaded_unit_show_count = 0
        self.commands = []

    @property
    def unit_cgroup(self):
        return f"{self.enclosing_cgroup}/app.slice/{self.unit_name}"

    def is_linux(self):
        return self.scenario != "non_linux"

    def pidfd_supported(self):
        return self.scenario != "pidfd_unavailable"

    def uid(self):
        return 1000

    def executable(self):
        return "/usr/bin/python3"

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def run(self, command, timeout):
        self.commands.append(list(command))
        if self.scenario == "command_missing" and command[0] == "loginctl":
            raise FileNotFoundError("loginctl")
        if self.scenario == "command_timeout" and command[0] == "loginctl":
            raise subprocess.TimeoutExpired(command, timeout)
        if command[0] == "loginctl":
            if self.scenario == "loginctl_failed":
                return subprocess.CompletedProcess(command, 1, "", "failed")
            linger = "no" if self.scenario == "linger_disabled" else "yes"
            return subprocess.CompletedProcess(command, 0, f"{linger}\n", "")
        if command[0] == "systemd-run":
            self.unit_name = next(part.split("=", 1)[1] for part in command if part.startswith("--unit="))
            if self.scenario == "transient_start_failed":
                return subprocess.CompletedProcess(command, 1, "", "failed")
            self.unit_started = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            return self._show_unit(command)
        if command[:3] == ["systemctl", "--user", "stop"]:
            self.stop_attempted = True
            if self.scenario != "cleanup_incomplete":
                self.unit_stopped = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "reset-failed"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["systemctl", "show"]:
            return self._show_enclosing(command)
        raise AssertionError(f"unexpected command: {command!r}")

    def _show_enclosing(self, command):
        self.enclosing_show_count += 1
        if self.scenario == "enclosing_unavailable":
            return subprocess.CompletedProcess(command, 1, "", "failed")
        invocation_id = self.enclosing_invocation_id
        if self.scenario == "enclosing_restarted" and self.enclosing_show_count > 1:
            invocation_id = "3" * 32
        kill_mode = "process" if self.scenario == "kill_mode_unsuitable" else "control-group"
        unit_id = "" if self.scenario == "enclosing_identity_missing" else "user@1000.service"
        manager_pid = "0" if self.scenario == "manager_identity_missing" else "111"
        output = "\n".join(
            [
                f"Id={unit_id}",
                f"InvocationID={invocation_id}",
                f"ControlGroup={self.enclosing_cgroup}",
                f"KillMode={kill_mode}",
                f"MainPID={manager_pid}",
            ]
        )
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    def _show_unit(self, command):
        if not self.unit_started:
            if self.scenario == "unexpected_unit_reuse":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "LoadState=loaded\nId=collision.service\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "LoadState=not-found\n", "")
        if self.unit_stopped:
            return subprocess.CompletedProcess(command, 0, "LoadState=not-found\n", "")
        self.loaded_unit_show_count += 1
        if self.scenario == "unit_show_failed" and self.loaded_unit_show_count == 2:
            return subprocess.CompletedProcess(command, 1, "", "failed")
        invocation_id = self.unit_invocation_id
        if self.scenario == "unit_identity_changed" and self.loaded_unit_show_count > 1:
            invocation_id = "4" * 32
        if self.scenario == "unit_identity_not_queryable" and self.released:
            invocation_id = "5" * 32
        unit_id = "" if self.scenario == "unit_identity_missing" else self.unit_name
        kill_mode = "process" if self.scenario == "unit_property_mismatch" else "control-group"
        main_pid = "0" if self.released else "222"
        output = "\n".join(
            [
                f"Id={unit_id}",
                f"InvocationID={invocation_id}",
                f"ControlGroup={'' if self.released else self.unit_cgroup}",
                f"MainPID={main_pid}",
                "ActiveState=active",
                f"SubState={'exited' if self.released else 'start'}",
                "LoadState=loaded",
                f"KillMode={kill_mode}",
                "RemainAfterExit=yes",
                "Type=oneshot",
            ]
        )
        return subprocess.CompletedProcess(command, 0, output + "\n", "")

    def read_text(self, path):
        path = str(path)
        if path == "/proc/sys/kernel/random/boot_id":
            return "not-a-boot-id\n" if self.scenario == "boot_id_missing" else f"{self.boot_id}\n"
        if path == "/proc/111/stat":
            self.manager_stat_count += 1
            if self.scenario == "process_identity_missing":
                return "malformed\n"
            ticks = 101 if self.scenario == "manager_identity_changed" and self.manager_stat_count > 1 else 100
            return self._proc_stat(111, ticks)
        if path == "/proc/111/cgroup":
            cgroup = (
                "/wrong"
                if self.scenario == "manager_identity_missing"
                else f"{self.enclosing_cgroup}/init.scope"
            )
            return f"0::{cgroup}\n"
        if path == "/proc/222/stat":
            self.helper_stat_count += 1
            ticks = 201 if self.scenario == "helper_identity_changed" and self.helper_stat_count > 1 else 200
            return self._proc_stat(222, ticks)
        if path == "/proc/222/cgroup":
            cgroup = "/wrong" if self.scenario == "helper_cgroup_mismatch" else self.unit_cgroup
            return f"0::{cgroup}\n"
        if path.endswith("/cgroup.events"):
            if self.unit_stopped and self.scenario != "cleanup_incomplete":
                raise FileNotFoundError(path)
            if self.scenario == "cgroup_events_unavailable":
                raise OSError("unavailable")
            if self.scenario == "cgroup_events_invalid":
                return "populated maybe\n"
            if self.scenario == "cgroup_drain_timeout":
                return "populated 1\n"
            if self.scenario == "cleanup_incomplete" and self.stop_attempted:
                return "populated 1\n"
            return f"populated {0 if self.released else 1}\n"
        raise AssertionError(f"unexpected read: {path}")

    @staticmethod
    def _proc_stat(pid, start_ticks):
        prefix_fields = ["S"] + ["0"] * 18
        return f"{pid} (agentteam probe) {' '.join(prefix_fields)} {start_ticks} 0\n"

    def make_state_dir(self):
        return Path("/tmp/fake-agentteam-invocation-probe")

    def release_helper(self, _gate_path):
        if self.scenario == "release_failed":
            raise OSError("release failed")
        self.released = True

    def remove_state_dir(self, _state_dir):
        self.state_removed = True

    def open_pidfd(self, _pid):
        if self.scenario == "pidfd_open_failed":
            raise OSError("pidfd failed")
        return 99

    def pidfd_exited(self, _pidfd, _timeout_seconds):
        return self.scenario != "helper_exit_timeout"

    def close_pidfd(self, _pidfd):
        self.pidfd_closed = True


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


def _full_run_model_invocation_events():
    events = []

    def add_start(
        invocation_id,
        stage,
        *,
        coverage_class="supported_model_invocation",
        task_id=None,
    ):
        record = {
            "invocation_schema_version": "model_invocation_started.v1",
            "invocation_id": invocation_id,
            "coverage_class": coverage_class,
            "usage_stage": stage,
            "task_id": task_id,
        }
        events.append(
            {
                "event_id": f"EVT-START-{invocation_id}",
                "event_type": "model_invocation_started",
                "source_event_id": invocation_id,
                "payload": record,
            }
        )
        return record

    def add_terminal(
        invocation_id,
        stage,
        usage_status,
        *,
        terminal_status="completed",
        provider_usage_scope="invocation",
        unavailable_reason=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        total_tokens=None,
    ):
        record = {
            "usage_schema_version": "model_invocation_usage.v1",
            "usage_event_id": f"USAGE-{invocation_id}",
            "invocation_id": invocation_id,
            "coverage_class": (
                "not_applicable_adapter"
                if usage_status == "not_applicable"
                else "supported_model_invocation"
            ),
            "usage_stage": stage,
            "terminal_status": terminal_status,
            "usage_status": usage_status,
            "provider_usage_scope": provider_usage_scope,
            "unavailable_reason": unavailable_reason,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
        }
        events.append(
            {
                "event_id": f"EVT-USAGE-{invocation_id}",
                "event_type": "model_invocation_usage_recorded",
                "source_event_id": record["usage_event_id"],
                "payload": record,
            }
        )
        return record

    author_start = add_start("INV-author", "taskpack_author")
    author_terminal = add_terminal(
        "INV-author",
        "taskpack_author",
        "reported",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=20,
        total_tokens=120,
    )
    add_start(
        "INV-worker-1",
        "implementation_worker",
        task_id="TASK-001",
    )
    add_terminal(
        "INV-worker-1",
        "implementation_worker",
        "reported",
        input_tokens=50,
        output_tokens=10,
        reasoning_tokens=3,
        total_tokens=60,
    )
    add_start(
        "INV-worker-2",
        "implementation_worker",
        task_id="TASK-001",
    )
    add_terminal(
        "INV-worker-2",
        "implementation_worker",
        "partial",
        provider_usage_scope="session_cumulative",
        unavailable_reason="provider_session_lineage_ambiguous",
        input_tokens=5000,
        total_tokens=6000,
    )
    add_start("INV-diagnostic", "runtime_diagnostic")
    add_terminal(
        "INV-diagnostic",
        "runtime_diagnostic",
        "partial",
        unavailable_reason="provider_payload_incomplete",
        input_tokens=7,
        total_tokens=9,
    )
    add_start("INV-smoke", "development_smoke")
    add_terminal(
        "INV-smoke",
        "development_smoke",
        "unavailable",
        terminal_status="timed_out",
        unavailable_reason="timeout_before_usage",
    )
    add_start("INV-acceptance", "acceptance_live_smoke")
    add_start(
        "INV-fake",
        "implementation_worker",
        coverage_class="not_applicable_adapter",
        task_id="TASK-FAKE",
    )
    add_terminal(
        "INV-fake",
        "implementation_worker",
        "not_applicable",
    )

    events.extend(
        [
            {
                "event_id": "EVT-START-author-REPLAY",
                "event_type": "model_invocation_started",
                "source_event_id": "INV-author",
                "payload": dict(author_start),
            },
            {
                "event_id": "EVT-USAGE-author-REPLAY",
                "event_type": "model_invocation_usage_recorded",
                "source_event_id": "USAGE-INV-author",
                "payload": dict(author_terminal),
            },
        ]
    )
    for sequence, event in enumerate(events, start=1):
        event["sequence"] = sequence
    return events


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


class _AuthorFakeGatedExecution:
    def __init__(
        self,
        lifecycle,
        command,
        *,
        cwd,
        input_text,
        timeout_seconds,
    ):
        self.command = list(command)
        self.cwd = str(cwd)
        self.input_text = input_text
        self.timeout_seconds = timeout_seconds

    def prepare(self):
        return ExecutionGroupIdentity(
            gated_supervisor_pid=4242,
            gated_supervisor_pgid=4242,
            host_boot_id="12345678-1234-1234-1234-123456789abc",
            gated_supervisor_start_ticks=9001,
            launch_nonce_sha256="a" * 64,
            systemd_linger_enabled=True,
            systemd_transient_unit="agentteam-inv-author-test.service",
            systemd_transient_invocation_id="b" * 32,
            systemd_transient_kill_mode="control-group",
            systemd_user_manager_identity="manager:test",
            systemd_transient_control_group=(
                "/user.slice/user-1000.slice/user@1000.service/"
                "app.slice/agentteam-inv-author-test.service"
            ),
            systemd_user_service_invocation_id="c" * 32,
            systemd_user_service_control_group=(
                "/user.slice/user-1000.slice/user@1000.service"
            ),
            systemd_user_service_kill_mode="control-group",
        )

    def permit_and_wait(
        self,
        *,
        progress_callback=None,
        progress_interval_seconds=30.0,
    ):
        try:
            completed = subprocess.run(
                self.command,
                cwd=self.cwd,
                input=self.input_text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProviderExecution(
                self.command,
                -9,
                _timeout_text(exc.stdout),
                _timeout_text(exc.stderr),
                timed_out=True,
            )
        return ProviderExecution(
            self.command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    def abort_before_permit(self):
        return None

    def cleanup_after_terminal(self):
        return None


def _timeout_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


REPO_ROOT = Path(__file__).resolve().parents[4]


def _phase2_controller_action_input(gate_id):
    digest = "0" * 64
    binding = {"path": "/fixture/authority.json", "sha256": digest}
    if gate_id == "P2-08":
        configuration = {
            "promoted_at": "2026-07-29T00:00:00Z",
            "readiness_reasons": {
                capability_id: "Validated fixture evidence."
                for capability_id in (
                    experiment_gates_module._CAPABILITY_TEST_IDS
                )
            },
            "capability_evidence": {
                capability_id: [
                    {
                        "artifact_path": "tests/fixture.py",
                        "sha256": digest,
                        "test_id": test_id,
                        "status": "passed",
                    }
                    for test_id in test_ids
                ]
                for capability_id, test_ids in (
                    experiment_gates_module
                    ._CAPABILITY_TEST_IDS.items()
                )
            },
            "authority_artifacts": {
                "protocol_template": dict(binding),
                "deterministic_calibration": dict(binding),
            },
            "pilot_mode": "agentteam_full",
            "pilot_repetition_index": 0,
            "pilot_stable_request_key": "phase2-pilot",
        }
    elif gate_id == "P2-09":
        configuration = {
            "authority_artifacts": {
                "evaluator": dict(binding),
            },
            "direct_taskpack": {
                "path": "/fixture/frozen-taskpack",
                "digest_sha256": digest,
            },
            "repeat_mode": "single_codex",
            "sandbox_configuration": {},
        }
    else:
        configuration = {
            "finalized_at": "2026-07-29T00:00:00Z",
        }
    return {
        "schema_version": "phase2_gate_action_input.v1",
        "action": {
            "P2-08": "promote_readiness",
            "P2-09": "run_live_calibration",
            "P2-10": "finalize_phase2",
        }[gate_id],
        "configuration": configuration,
    }


def _phase2_controller_gate_declarations():
    schema_prefix = "experiments/native_agentteam_runtime/schemas/"
    definitions = (
        (
            "P2-08",
            [],
            "acceptance/readiness.json",
            "phase2_readiness_promotion.schema.json",
            "phase2_readiness_controller_v1",
            "phase2_readiness_relation_v1",
            False,
        ),
        (
            "P2-09",
            ["P2-08"],
            "acceptance/calibration.json",
            "phase2_calibration.schema.json",
            "phase2_live_calibration_controller_v1",
            "phase2_live_calibration_relation_v1",
            True,
        ),
        (
            "P2-10",
            ["P2-09"],
            "acceptance/finalization.json",
            "phase2_finalization.schema.json",
            "phase2_finalization_controller_v1",
            "phase2_finalization_relation_v1",
            False,
        ),
    )
    declarations = []
    for (
        gate_id,
        dependencies,
        evidence_artifact,
        evidence_schema,
        controller_entrypoint,
        relation_validator,
        authorization_required,
    ) in definitions:
        declaration = {
            "gate_id": gate_id,
            "depends_on": dependencies,
            "executor": "deterministic_controller",
            "evidence_artifact": evidence_artifact,
            "evidence_schema": schema_prefix + evidence_schema,
            "required_status_field": "controller_validation_status",
            "required_status_value": "passed",
            "controller_entrypoint": controller_entrypoint,
            "relation_validator": relation_validator,
            "operator_authorization_required": authorization_required,
            "controller_action_input": (
                _phase2_controller_action_input(gate_id)
            ),
        }
        if authorization_required:
            declaration.update(
                {
                    "operator_authorization_schema": (
                        schema_prefix
                        + "phase2_live_authorization.schema.json"
                    ),
                    "operator_authorization_required_decision": "approved",
                }
            )
        declarations.append(declaration)
    return declarations


class TaskpackTests(unittest.TestCase):
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
            authority_root = repo / "authority"
            authority_root.mkdir()
            protocol_template = authority_root / "protocol.json"
            deterministic_calibration = (
                authority_root / "deterministic-calibration.json"
            )
            evaluator = authority_root / "evaluator.json"
            for path, value in (
                (protocol_template, {"protocol": "fixture"}),
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

            def authority_binding(path):
                return {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest(),
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
                        "authority_artifacts"
                    ] = {
                        "protocol_template": authority_binding(
                            protocol_template
                        ),
                        "deterministic_calibration": authority_binding(
                            deterministic_calibration
                        ),
                    }
                elif gate["gate_id"] == "P2-09":
                    gate["controller_action_input"]["configuration"][
                        "authority_artifacts"
                    ] = {
                        "evaluator": authority_binding(evaluator),
                    }
                    gate["controller_action_input"]["configuration"][
                        "direct_taskpack"
                    ] = {
                        "path": str(direct_taskpack_path.resolve()),
                        "digest_sha256": direct_taskpack_digest,
                    }
            blueprint = {
                "schema_version": "agentteam_taskpack_blueprint.v1",
                "blueprint_id": "example-blueprint",
                "source_plan": source_plan_relative,
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
                frozen = freeze_taskpack(
                    result["taskpack_dir"],
                    tmp_path / "frozen",
                )

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

    def test_blueprint_freeze_rejects_each_drifted_materialized_artifact(self):
        for artifact_name in (
            "taskpack.yaml",
            "agent_pool.json",
            "backlog.json",
            "verification.json",
            "README.md",
        ):
            with self.subTest(artifact_name=artifact_name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    _init_repo(repo)
                    blueprint_path, _blueprint = _blueprint_fixture(repo)
                    materialized = (
                        taskpack_module.materialize_taskpack_blueprint(
                            repo,
                            blueprint_path,
                            tmp_path / "drafts",
                        )
                    )
                    artifact_path = (
                        Path(materialized["taskpack_dir"]) / artifact_name
                    )
                    artifact_path.write_text(
                        artifact_path.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        TaskpackValidationError,
                        (
                            "blueprint-materialized taskpack artifact changed "
                            f"before freeze: {re.escape(artifact_name)}"
                        ),
                    ):
                        freeze_taskpack(
                            materialized["taskpack_dir"],
                            tmp_path / "frozen",
                        )

                    self.assertFalse(
                        (
                            tmp_path
                            / "frozen"
                            / "example-blueprint"
                        ).exists()
                    )

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

    def test_blueprint_release_git_object_binding_and_generation_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["approval"]["runtime_release_binding_required"] = True
            blueprint["approval"]["pre04_ancestor_binding_required"] = True
            _write_json(repo / blueprint_path, blueprint)
            record = _write_blueprint_approval(repo, blueprint)
            pre04_integration_commit = subprocess.run(
                ["git", "rev-parse", "HEAD^"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            record["pre04_integration_commit"] = pre04_integration_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            release_id = record["preflight_release_id"]
            release_source_commit = record["preflight_release_source_commit"]
            _write_json(
                repo / ".agentteam" / "profile.json",
                {"work_root": str(work_root)},
            )
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            output_root = tmp_path / "drafts"

            with mock.patch.object(
                taskpack_module,
                "validate_taskpack",
                side_effect=TaskpackValidationError("injected generated-package failure"),
            ):
                with self.assertRaises(TaskpackValidationError):
                    taskpack_module.materialize_taskpack_blueprint(
                        repo,
                        blueprint_path,
                        output_root,
                    )
            self.assertFalse(output_root.exists())

            tree_oid = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            unrelated_commit = subprocess.run(
                ["git", "commit-tree", tree_oid, "-m", "unrelated PRE-04"],
                cwd=repo,
                env=_test_env(),
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()

            record["preflight_release_source_commit"] = unrelated_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": unrelated_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": unrelated_commit,
                },
            )
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unrelated-release-unused",
                dry_run=True,
            )
            self.assertTrue(
                any(
                    "not an ancestor of the blueprint source commit" in detail
                    for detail in dry_result["approval_diagnostics"]
                )
            )

            record["preflight_release_source_commit"] = release_source_commit
            _write_json(
                work_root / "releases" / "active.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            _write_json(
                work_root / "releases" / release_id / "manifest.json",
                {
                    "release_id": release_id,
                    "source_git_commit": release_source_commit,
                },
            )
            record["pre04_integration_commit"] = unrelated_commit
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unrelated-pre04-unused",
                dry_run=True,
            )
            self.assertTrue(
                any(
                    "not an ancestor" in detail
                    for detail in dry_result["approval_diagnostics"]
                )
            )

            record["pre04_integration_commit"] = "0" * 40
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "pre04-unused",
                dry_run=True,
            )
            self.assertTrue(
                any("PRE-04" in detail for detail in dry_result["approval_diagnostics"])
            )

            record["pre04_integration_commit"] = pre04_integration_commit
            record["preflight_release_source_commit"] = "0" * 64
            _write_json(repo / blueprint["approval"]["record_path"], record)
            dry_result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )
            self.assertTrue(
                any("Git OID" in detail for detail in dry_result["approval_diagnostics"])
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

    def test_blueprint_accepts_only_unique_ancestor_generated_input_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            generated_path = ".agentteam/generated/generated_analysis.json"
            blueprint["tasks"][0]["expected_output_artifacts"] = [generated_path]
            blueprint["tasks"][1]["input_artifacts"].append(generated_path)
            _write_json(repo / blueprint_path, blueprint)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )
            self.assertEqual(result["validation_status"], "accepted")

            blueprint["tasks"][1]["depends_on"] = []
            _write_json(repo / generated_path, {"stale": True})
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "not produced by exactly one ancestor task",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "missing-edge",
                    dry_run=True,
                )

            blueprint["tasks"][1]["depends_on"] = ["T-1"]
            blueprint["tasks"][2]["expected_output_artifacts"] = [generated_path]
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "expected_output_artifacts must have one producer",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "duplicate-producer",
                    dry_run=True,
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

    def test_blueprint_materialization_rejects_authority_drift_from_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["tasks"][0]["objective"] = "Uncommitted authority drift."
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "blueprint_path must match the committed HEAD bytes",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "drafts",
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

    def _run_agentteam_json(self, *args):
        completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", *args, "--json"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def _projection_parity_payload(self, command, summary):
        if command == "status":
            return {
                "latest_run": summary["latest_run"],
                "run_status": summary["run_status"],
                "overall_status": summary["overall_status"],
                "tasks": summary["tasks"],
                "manual_gates": summary["manual_gates"],
                "permission_requests": summary["permission_requests"],
            }
        if command == "logs":
            return {
                "latest_run": summary["latest_run"],
                "event_count": summary["event_count"],
                "returned_count": summary["returned_count"],
                "events": [
                    {
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "sequence": event.get("sequence"),
                        "payload": event.get("payload"),
                    }
                    for event in summary["events"]
                ],
            }
        if command == "report":
            return {
                "run_id": summary["run_id"],
                "run_status": summary["run_status"],
                "task_count": summary["task_count"],
                "blocked_count": summary["blocked_count"],
                "token_usage": summary["token_usage"],
                "task_reports": summary["operator_report"]["task_reports"],
            }
        if command == "taskpack_list":
            return {
                "frozen_count": summary["frozen_count"],
                "taskpacks": [
                    {
                        "taskpack_id": item["taskpack_id"],
                        "goal": item.get("goal"),
                        "run_status": item["run_status"],
                        "run_dir": item.get("run_dir"),
                    }
                    for item in summary["taskpacks"]
                ],
            }
        raise AssertionError(f"unknown projection parity command: {command}")

    def assertProjectionFreshMetadata(self, summary):
        self.assertEqual(summary.get("projection_source"), "db")
        self.assertEqual(summary.get("projection_status"), "fresh")
        self.assertTrue(summary.get("projection_db_path"))
        self.assertNotIn("projection_warning", summary)

    def assertProjectionFallbackHealthMetadata(self, summary, projection_status):
        self.assertEqual(summary.get("projection_source"), "files")
        self.assertEqual(summary.get("projection_status"), projection_status)
        self.assertEqual(summary.get("projection_warning"), "projection_db_unavailable")
        self.assertTrue(summary.get("projection_db_path"))

    def assertProjectionFallbackMetadata(self, summary, projection_status):
        self.assertProjectionFallbackHealthMetadata(summary, projection_status)
        self.assertEqual(summary.get("next_action"), "run agentteam db rebuild")
        self.assertEqual(summary.get("operator_hint"), "agentteam db rebuild")

    def test_project_projection_db_rebuild_indexes_runs_taskpacks_events_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "task_id": "optimize-pipeline",
                    "result": {
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "incomplete",
                        "trace_carrier": [{"type": "event", "path": "EVT-0001"}],
                        "missing_evidence": ["review_result"],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            summary = rebuild_project_projection_db(work_root)

            self.assertEqual(summary["db_status"], "rebuilt")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["tasks"], 1)
            self.assertEqual(summary["evidence"], {"incomplete": 1})
            db_path = Path(summary["db_path"])
            with sqlite3.connect(db_path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                run_rows = connection.execute(
                    "select run_id, event_count, latest_event_type from runs"
                ).fetchall()
                evidence_rows = connection.execute(
                    "select run_id, task_id, attempt_id, evidence_level, evidence_status, missing_evidence_json from evidence_summaries"
                ).fetchall()

            self.assertTrue(
                {
                    "schema_info",
                    "runs",
                    "taskpacks",
                    "events",
                    "tasks",
                    "evidence_summaries",
                }.issubset(table_names)
            )
            self.assertEqual(run_rows, [("projection-run", 1, "run_completed")])
            self.assertEqual(
                evidence_rows,
                [
                    (
                        "projection-run",
                        "optimize-pipeline",
                        "optimize-pipeline-ATTEMPT-001",
                        "L2",
                        "incomplete",
                        '["review_result"]',
                    )
                ],
            )

    def test_project_projection_db_check_detects_stale_event_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            rebuild_project_projection_db(work_root)

            passed = check_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            failed = check_project_projection_db(work_root)

            self.assertEqual(passed["check_status"], "passed")
            self.assertEqual(failed["check_status"], "failed")
            self.assertIn("events", failed["mismatches"])
            self.assertEqual(failed["expected"]["events"], 2)
            self.assertEqual(failed["actual"]["events"], 1)

    def test_project_projection_db_check_reports_readthrough_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            taskpack_path = work_root / "frozen" / "projection-run" / "taskpack.json"
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection freshness fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            missing = check_project_projection_db(work_root)
            self.assertEqual(missing.get("projection_source"), "files")
            self.assertEqual(missing.get("projection_status"), "missing")
            self.assertEqual(missing.get("projection_warning"), "projection_db_unavailable")
            self.assertEqual(missing.get("next_action"), "run agentteam db rebuild")
            self.assertEqual(missing.get("operator_hint"), "agentteam db rebuild")
            self.assertEqual(missing.get("projection_db_path"), missing["db_path"])
            self.assertIn("db_missing", missing["mismatches"])

            rebuild_project_projection_db(work_root)
            fresh = check_project_projection_db(work_root)
            self.assertEqual(fresh.get("projection_source"), "db")
            self.assertEqual(fresh.get("projection_status"), "fresh")
            self.assertEqual(fresh.get("projection_db_path"), fresh["db_path"])
            self.assertNotIn("projection_warning", fresh)
            self.assertEqual(fresh["mismatches"], [])

            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            stale_event = check_project_projection_db(work_root)
            self.assertEqual(stale_event.get("projection_source"), "files")
            self.assertEqual(stale_event.get("projection_status"), "stale")
            self.assertEqual(stale_event.get("projection_warning"), "projection_db_unavailable")
            self.assertEqual(stale_event.get("next_action"), "run agentteam db rebuild")
            self.assertIn("events", stale_event["mismatches"])

            rebuild_project_projection_db(work_root)
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection freshness fixture changed.",
                    "validation": {"status": "accepted"},
                },
            )
            stale_taskpack = check_project_projection_db(work_root)
            self.assertEqual(stale_taskpack.get("projection_source"), "files")
            self.assertEqual(stale_taskpack.get("projection_status"), "stale")
            self.assertIn("artifact_digest", stale_taskpack["mismatches"])

            rebuild_project_projection_db(work_root)
            db_path = Path(fresh["db_path"])
            db_path.write_text("not sqlite", encoding="utf-8")
            corrupt = check_project_projection_db(work_root)
            self.assertEqual(corrupt.get("projection_source"), "files")
            self.assertEqual(corrupt.get("projection_status"), "corrupt")
            self.assertEqual(corrupt.get("projection_warning"), "projection_db_unavailable")
            self.assertIn("db_unreadable", corrupt["mismatches"])

    def test_projected_readers_can_report_normalized_fallback_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection reader fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            taskpacks = projection_db.read_projected_taskpacks(work_root)
            events = projection_db.read_projected_run_events(work_root, "projection-run")
            self.assertEqual(taskpacks["projection_source"], "db")
            self.assertEqual(taskpacks.get("projection_status"), "fresh")
            self.assertEqual(events["projection_source"], "db")
            self.assertEqual(events.get("projection_status"), "fresh")
            self.assertEqual([event["event_id"] for event in events["events"]], ["EVT-0001"])

            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            self.assertIsNone(projection_db.read_projected_taskpacks(work_root))
            self.assertIsNone(projection_db.read_projected_run_events(work_root, "projection-run"))
            try:
                taskpack_fallback = projection_db.read_projected_taskpacks(
                    work_root,
                    include_fallback_status=True,
                )
                event_fallback = projection_db.read_projected_run_events(
                    work_root,
                    "projection-run",
                    include_fallback_status=True,
                )
            except TypeError as exc:
                self.fail(f"projection readers should expose fallback status: {exc}")

            self.assertEqual(taskpack_fallback["projection_source"], "files")
            self.assertEqual(taskpack_fallback["projection_status"], "stale")
            self.assertEqual(taskpack_fallback["projection_warning"], "projection_db_unavailable")
            self.assertEqual(taskpack_fallback["next_action"], "run agentteam db rebuild")
            self.assertNotIn("taskpacks", taskpack_fallback)
            self.assertEqual(event_fallback["projection_source"], "files")
            self.assertEqual(event_fallback["projection_status"], "stale")
            self.assertNotIn("events", event_fallback)

            Path(taskpack_fallback["db_path"]).unlink()
            missing_fallback = projection_db.read_projected_taskpacks(
                work_root,
                include_fallback_status=True,
            )
            self.assertEqual(missing_fallback["projection_status"], "missing")

            Path(missing_fallback["db_path"]).write_text("not sqlite", encoding="utf-8")
            corrupt_fallback = projection_db.read_projected_run_events(
                work_root,
                "projection-run",
                include_fallback_status=True,
            )
            self.assertEqual(corrupt_fallback["projection_status"], "corrupt")
            self.assertIn("db_unreadable", corrupt_fallback["check"]["mismatches"])

    def test_project_projection_db_rebuild_indexes_artifacts_and_run_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "task_id": "optimize-pipeline",
                    "result": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [{"type": "file", "path": "reports/final_report.md"}],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "projection-run"})
            report_markdown = run_dir / "reports" / "final_report.md"
            report_markdown.parent.mkdir(parents=True, exist_ok=True)
            report_markdown.write_text("completed report\n", encoding="utf-8")
            patch_path = run_dir / "steps" / "STEP-0001-optimize-pipeline" / "result.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            taskpack_path = work_root / "frozen" / "projection-run" / "taskpack.json"
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            summary = rebuild_project_projection_db(work_root)

            self.assertGreaterEqual(summary["artifacts"], 7)
            self.assertEqual(summary["run_stats"], 1)
            self.assertGreater(summary["artifact_bytes"], 0)
            db_path = Path(summary["db_path"])
            expected_report_sha = hashlib.sha256(
                report_markdown.read_bytes()
            ).hexdigest()
            with sqlite3.connect(db_path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                artifact_types = {
                    row[0]
                    for row in connection.execute(
                        "select distinct artifact_type from artifacts"
                    ).fetchall()
                }
                report_row = connection.execute(
                    """
                    select size_bytes, sha256, retention_policy
                    from artifacts
                    where path = ?
                    """,
                    (str(report_markdown.resolve()),),
                ).fetchone()
                stats_row = connection.execute(
                    """
                    select run_id, input_tokens, output_tokens, total_tokens,
                           artifact_count, artifact_bytes
                    from run_stats
                    where run_id = ?
                    """,
                    ("projection-run",),
                ).fetchone()
                evidence_row = connection.execute(
                    """
                    select content_size_bytes, content_sha256, source_path
                    from evidence_summaries
                    where run_id = ? and task_id = ?
                    """,
                    ("projection-run", "optimize-pipeline"),
                ).fetchone()

            self.assertTrue({"artifacts", "run_stats"}.issubset(table_names))
            self.assertTrue(
                {
                    "event_log",
                    "report",
                    "patch",
                    "taskpack",
                    "state_snapshot",
                    "role_context",
                    "repo_context",
                }.issubset(artifact_types)
            )
            self.assertEqual(
                report_row,
                (len("completed report\n".encode("utf-8")), expected_report_sha, "authoritative"),
            )
            self.assertEqual(stats_row[:4], ("projection-run", 1200, 300, 1500))
            self.assertGreaterEqual(stats_row[4], 7)
            self.assertGreater(stats_row[5], 0)
            self.assertGreater(evidence_row[0], 0)
            self.assertEqual(len(evidence_row[1]), 64)
            self.assertEqual(evidence_row[2], str(state_path.resolve()))

    def test_project_projection_db_projects_follow_up_lineage_and_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            attempt_id = "optimize-pipeline-ATTEMPT-001"
            patch_path = run_dir / "steps" / "STEP-0001-optimize-pipeline" / "result.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
            verification_addition = {
                "label": "projection-readthrough",
                "command": ["python3", "-m", "unittest", "tests.test_projection"],
                "reason": "cover the DB projection query surface",
            }
            verification_addition_result = {
                **verification_addition,
                "verification_addition_status": "passed",
                "verification_addition_exit_code": 0,
                "verification_addition_stdout": "ok\n",
                "verification_addition_stderr": "",
                "verification_addition_rejection_reason": None,
            }
            runtime_output = {
                "operator_summary": {
                    "what_changed": "投影 follow-up lineage。",
                    "verification_summary": "focused projection test passed",
                    "measured_result": "worker verification_additions preserved",
                    "merge_recommendation": "人工审阅后合并。",
                    "next_steps": ["补充 DB 投影 readthrough 测试。"],
                },
                "verification_additions": [verification_addition],
            }
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "step_id": "STEP-0001-optimize-pipeline",
                    "task_id": "optimize-pipeline",
                    "result": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": attempt_id,
                        "lease_id": "LEASE-001",
                        "changed_files": ["agentteam_runtime/projection_db.py"],
                        "runtime_output": runtime_output,
                        "validation_status": "accepted",
                        "failure_category": None,
                        "patch_path": str(patch_path),
                        "integration_queue_status": "verified",
                        "integration_queue_item_id": f"optimize-pipeline:{attempt_id}",
                        "integration_status": "applied",
                        "integration_worktree_path": str(run_dir / "integration" / "optimize-pipeline"),
                        "integration_verification_status": "passed",
                        "integration_verification_exit_code": 0,
                        "integration_verification_stdout": "primary ok\n",
                        "integration_verification_stderr": "",
                        "integration_verification_additions_status": "passed",
                        "integration_verification_additions": [verification_addition_result],
                        "integration_commit_status": "not_requested",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [{"type": "command", "command": "focused", "result": "passed"}],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(
                run_dir / "codex_results" / f"codex_result_{attempt_id}.json",
                {
                    "result_status": "completed",
                    "changed_files": ["agentteam_runtime/projection_db.py"],
                    "output": runtime_output,
                },
            )
            _write_json(
                run_dir / "state" / "integration_queue.json",
                {
                    "queue_schema_version": "integration_queue.v1",
                    "items": [
                        {
                            "queue_item_id": f"optimize-pipeline:{attempt_id}",
                            "queue_status": "verified",
                            "task_id": "optimize-pipeline",
                            "attempt_id": attempt_id,
                            "lease_id": "LEASE-001",
                            "patch_path": str(patch_path),
                            "integration_status": "applied",
                            "integration_verification_status": "passed",
                            "integration_verification_exit_code": 0,
                            "integration_verification_additions_status": "passed",
                            "integration_verification_additions": [verification_addition_result],
                            "integration_commit_status": "not_requested",
                        }
                    ],
                },
            )
            events = _read_jsonl(run_dir / "events.jsonl")
            events.append(
                {
                    "event_id": "EVT-0002",
                    "event_type": "integration_verified",
                    "sequence": 2,
                    "payload": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": attempt_id,
                        "lease_id": "LEASE-001",
                        "integration_verification_status": "passed",
                        "integration_verification_exit_code": 0,
                        "integration_verification_stdout": "primary ok\n",
                        "integration_verification_stderr": "",
                        "integration_verification_additions_status": "passed",
                        "integration_verification_additions": [verification_addition_result],
                    },
                }
            )
            _write_jsonl(run_dir / "events.jsonl", events)
            goal_memory_path = work_root / "pursue" / "projection-goal-memory.json"
            _write_json(
                goal_memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(goal_memory_path),
                    "latest_taskpack_id": "projection-run",
                    "latest_run_ids": ["projection-run"],
                    "follow_up_queue": [
                        {
                            "objective": "补充 DB 投影 readthrough 测试。",
                            "source_taskpack_id": "projection-run",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "source_result_status": "completed",
                            "source_run_outcome": "completed_with_review_required",
                            "source_evidence_paths": [
                                {"type": "report", "path": str(run_dir / "reports" / "final_report.md")}
                            ],
                            "stop_reason": "review_gate_required",
                            "suggested_verification": "python3 -m unittest tests.test_projection",
                        }
                    ],
                },
            )

            summary = rebuild_project_projection_db(work_root)
            lineage = projection_db.read_projected_follow_up_lineage(work_root)

            self.assertEqual(summary["follow_up_items"], 1)
            self.assertEqual(summary["worker_results"], 1)
            self.assertEqual(summary["integration_outcomes"], 1)
            self.assertEqual(summary["worker_verification_additions"], 2)
            self.assertEqual(lineage["projection_source"], "db")
            self.assertEqual(lineage["projection_status"], "fresh")
            self.assertEqual(
                lineage["follow_up_items"][0]["objective"],
                "补充 DB 投影 readthrough 测试。",
            )
            self.assertTrue(lineage["follow_up_items"][0]["selected_next_goal"])
            self.assertEqual(
                lineage["follow_up_items"][0]["goal_memory_path"],
                str(goal_memory_path.resolve()),
            )
            self.assertEqual(lineage["worker_results"][0]["result_status"], "completed")
            self.assertEqual(
                lineage["worker_results"][0]["verification_additions"][0]["command"],
                ["python3", "-m", "unittest", "tests.test_projection"],
            )
            self.assertEqual(
                lineage["integration_outcomes"][0]["integration_verification_status"],
                "passed",
            )
            self.assertEqual(
                lineage["integration_outcomes"][0]["verification_additions"][0][
                    "verification_addition_status"
                ],
                "passed",
            )
            with sqlite3.connect(summary["db_path"]) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
            self.assertTrue(
                {
                    "follow_up_items",
                    "worker_results",
                    "integration_outcomes",
                    "worker_verification_additions",
                }.issubset(table_names)
            )

    def test_project_projection_db_check_detects_artifact_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            report_path = run_dir / "reports" / "final_report.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("alpha\n", encoding="utf-8")
            rebuild_project_projection_db(work_root)

            report_path.write_text("bravo\n", encoding="utf-8")
            failed = check_project_projection_db(work_root)

            self.assertEqual(failed["check_status"], "failed")
            self.assertIn("artifact_digest", failed["mismatches"])

    def test_project_stats_reads_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "task_id": "optimize-pipeline",
                    "result": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            stats = projection_db.build_project_stats(work_root)

            self.assertEqual(stats["projection_source"], "db")
            self.assertEqual(stats["check_status"], "passed")
            self.assertEqual(stats["runs"], 1)
            self.assertEqual(stats["taskpacks"], 1)
            self.assertEqual(stats["events"], 1)
            self.assertEqual(stats["tasks"], 1)
            self.assertEqual(stats["evidence"], {"complete": 1})
            self.assertGreaterEqual(stats["artifacts"]["total_count"], 4)
            self.assertGreater(stats["artifacts"]["total_bytes"], 0)
            self.assertEqual(stats["token_usage"]["total_tokens"], 1500)

    def test_project_stats_falls_back_to_files_when_projection_db_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            stats = projection_db.build_project_stats(work_root)

            self.assertEqual(stats["projection_source"], "files")
            self.assertEqual(stats["check_status"], "failed")
            self.assertEqual(stats["runs"], 1)
            self.assertEqual(stats["taskpacks"], 1)
            self.assertEqual(stats["events"], 1)
            self.assertEqual(stats["tasks"], 1)
            self.assertEqual(stats["projection_warning"], "projection_db_unavailable")
            self.assertEqual(stats["next_action"], "run agentteam db rebuild")
            self.assertGreaterEqual(stats["artifacts"]["total_count"], 4)
            self.assertEqual(stats["token_usage"]["total_tokens"], 1500)

    def test_project_artifact_retention_plan_reads_rebuildable_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1", "notes": ["small"]},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1", "selected_files": ["a.py"]},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            plan = projection_db.read_projected_artifact_retention_plan(work_root, limit=1)

            self.assertEqual(plan["projection_source"], "db")
            self.assertEqual(plan["plan_status"], "ready")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual(len(plan["candidates"]), 1)
            self.assertEqual(plan["candidates"][0]["retention_policy"], "rebuildable")
            self.assertIn(
                plan["candidates"][0]["artifact_type"],
                {"role_context", "repo_context"},
            )
            self.assertGreaterEqual(plan["retention_policies"]["authoritative"], 1)
            self.assertGreaterEqual(plan["retention_policies"]["rebuildable"], 2)

    def test_projected_artifact_retention_plan_validates_candidate_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1", "notes": ["small"]},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1", "selected_files": ["a.py"]},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            plan = projection_db.read_projected_artifact_retention_plan(work_root, limit=1)

            self.assertEqual(plan["validation_status"], "passed")
            self.assertEqual(plan["validated_candidate_count"], 2)
            self.assertEqual(plan["invalid_candidate_count"], 0)
            self.assertEqual(plan["invalid_candidates"], [])
            self.assertEqual(plan["candidates"][0]["validation"]["status"], "passed")
            self.assertTrue(plan["candidates"][0]["validation"]["sha256_matches"])
            self.assertTrue(plan["candidates"][0]["validation"]["size_matches"])

    def test_project_artifact_retention_plan_returns_none_without_fresh_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )

            plan = projection_db.read_projected_artifact_retention_plan(work_root)

            self.assertIsNone(plan)

    def test_agentteam_cli_db_rebuild_and_check_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "projection-cli")
            _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            rebuild_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "db",
                    "rebuild",
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
            check_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "db",
                    "check",
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

            self.assertEqual(rebuild_completed.returncode, 0, rebuild_completed.stderr)
            self.assertEqual(check_completed.returncode, 0, check_completed.stderr)
            rebuild_summary = json.loads(rebuild_completed.stdout)
            check_summary = json.loads(check_completed.stdout)
            self.assertEqual(rebuild_summary["db_status"], "rebuilt")
            self.assertEqual(rebuild_summary["project"], "projection-cli")
            self.assertEqual(rebuild_summary["runs"], 1)
            self.assertEqual(check_summary["check_status"], "passed")
            self.assertEqual(check_summary["db_path"], rebuild_summary["db_path"])

    def test_agentteam_cli_stats_json_reads_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "stats-cli")
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stats",
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
            self.assertEqual(summary["project"], "stats-cli")
            self.assertEqual(summary["projection_source"], "db")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["token_usage"]["total_tokens"], 1500)

    def test_agentteam_cli_stats_json_falls_back_to_files_without_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "stats-cli")
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stats",
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
            self.assertEqual(summary["projection_source"], "files")
            self.assertEqual(summary["check_status"], "failed")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["token_usage"]["total_tokens"], 1500)

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

    def test_completion_summary_does_not_treat_explicit_no_change_task_as_missing_files(self):
        summary = build_completion_summary(
            run_id="audit-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "AUDIT-001",
                    "status": "investigation completed",
                    "work_type": "code_investigation",
                    "what_changed": ["Reviewed the run and found no safe in-repo code change."],
                    "changed_files": [],
                    "verification": ["read-only audit completed"],
                    "integration": "not_applicable",
                    "no_code_changes_required": True,
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertNotIn("No changed files were reported.", summary["evidence_gaps"])
        self.assertEqual(summary["changed_files_note"], "No source files changed; this task was completed as a no-change investigation.")
        self.assertEqual(summary["changed_files_note_zh"], "未修改源文件；该任务是无需代码变更的调查任务。")
        self.assertIn("Changed files: No source files changed; this task was completed as a no-change investigation.", lines)
        self.assertIn(
            "涉及文件：未修改源文件；该任务是无需代码变更的调查任务。",
            summary["chinese_operator_brief"],
        )

    def test_completion_summary_reports_evidence_status_counts(self):
        summary = build_completion_summary(
            run_id="evidence-run",
            run_status="completed",
            task_count=2,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                    "what_changed": ["Changed one file."],
                    "changed_files": ["src/a.py"],
                    "verification": ["unit: passed"],
                    "integration": "passed",
                    "evidence_status": "complete",
                },
                {
                    "task_id": "TASK-002",
                    "status": "implementation completed, integration blocked",
                    "what_changed": ["Changed another file."],
                    "changed_files": ["src/b.py"],
                    "verification": ["unit: passed"],
                    "integration": "blocked",
                    "evidence_status": "incomplete",
                    "missing_evidence": ["review_result"],
                },
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertEqual(
            summary["evidence_status_counts"],
            {"complete": 1, "incomplete": 1, "blocked": 0, "escalated": 0},
        )
        self.assertIn("Evidence status:", lines)
        self.assertIn("- incomplete: 1", lines)

    def test_completion_summary_builds_deterministic_chinese_operator_brief(self):
        summary = build_completion_summary(
            run_id="brief-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                    "next_steps": ["Run full validation."],
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertEqual(
            summary["chinese_operator_brief"],
            [
                "本次运行已完成，共 1 个任务，0 个阻塞。",
                "主要变更：Optimized the gesture scoring pipeline.",
                "涉及文件：gesture_recognition/sim_eval.py",
                "验证情况：unit_tests: passed",
                "集成状态：已通过",
                "合并建议：Review accepted patch before merging.",
                "下一步：Run full validation.",
                (
                    "下一步原因：The run completed with a recommended next implementation step."
                    "；相关下一步：Run full validation."
                ),
            ],
        )
        self.assertIn("中文简报:", lines)
        self.assertIn("- 本次运行已完成，共 1 个任务，0 个阻塞。", lines)

    def test_completion_summary_includes_chinese_operator_digest(self):
        summary = build_completion_summary(
            run_id="taskpack-7",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "optimize-pipeline",
                    "status": "implementation completed",
                    "what_changed": ["优化了手势评分流水线的数据窗口复制。"],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "measured_result": ["算法窗口复制阶段耗时下降 2%。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                    "next_steps": ["在比赛 QEMU 环境复测端到端延迟。"],
                }
            ],
            integration_baseline={"branch": "agentteam/run/taskpack-7/integration"},
        )

        self.assertEqual(
            summary["operator_digest"],
            [
                "做了什么：优化了手势评分流水线的数据窗口复制。",
                "涉及文件：gesture_recognition/sim_eval.py",
                "验证结果：unit_tests: passed",
                "实际结果：算法窗口复制阶段耗时下降 2%。",
                "风险：存在 review gate；source merge、push、release activation 仍需 operator 审阅。",
                "合并建议：Review accepted patch before merging.",
                "下一步：在比赛 QEMU 环境复测端到端延迟。",
                (
                    "下一步原因：Accepted changes have an integration baseline and the worker "
                    "recommended a next step.；相关下一步：在比赛 QEMU 环境复测端到端延迟。"
                ),
            ],
        )
        lines = []
        extend_completion_summary_lines(lines, summary)
        self.assertIn("中文工作汇报:", lines)
        self.assertIn("- 做了什么：优化了手势评分流水线的数据窗口复制。", lines)

    def test_completion_summary_aggregates_multiple_tasks_in_operator_digest(self):
        summary = build_completion_summary(
            run_id="multi-task-run",
            run_status="completed",
            task_count=2,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-A",
                    "status": "implementation completed",
                    "what_changed": ["实现任务 A 的代码路径。"],
                    "changed_files": ["src/a.py"],
                    "verification": ["unit-a: passed"],
                    "measured_result": ["A 延迟下降 2%。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch A.",
                    "next_steps": ["继续验证 A。"],
                },
                {
                    "task_id": "TASK-B",
                    "status": "implementation completed",
                    "what_changed": ["补充任务 B 的验证入口。"],
                    "changed_files": ["src/b.py"],
                    "verification": ["unit-b: passed"],
                    "measured_result": ["B 报告覆盖两个任务。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch B.",
                    "next_steps": ["继续验证 B。"],
                },
            ],
        )

        self.assertEqual(
            summary["operator_digest"],
            [
                "做了什么：实现任务 A 的代码路径。；补充任务 B 的验证入口。",
                "涉及文件：src/a.py；src/b.py",
                "验证结果：unit-a: passed；unit-b: passed",
                "实际结果：A 延迟下降 2%。；B 报告覆盖两个任务。",
                "合并建议：Review accepted patch A.；Review accepted patch B.",
                "下一步：继续验证 A。；继续验证 B。",
                (
                    "下一步原因：The run completed with a recommended next implementation step."
                    "；相关下一步：继续验证 A。"
                ),
            ],
        )

    def test_completion_summary_includes_follow_up_recommendation(self):
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
                    "next_steps": ["在比赛 QEMU 环境复测端到端延迟。"],
                }
            ],
            integration_baseline={"branch": "agentteam/run/taskpack-7/integration"},
        )

        recommendation = summary["follow_up_recommendation"]
        self.assertEqual(recommendation["action"], "integrate_then_next")
        self.assertEqual(recommendation["integrate_command"], "agentteam integrate --taskpack taskpack-7")
        self.assertEqual(
            recommendation["next_command"],
            'agentteam next --from-taskpack taskpack-7 --goal "在比赛 QEMU 环境复测端到端延迟。"',
        )
        lines = []
        extend_completion_summary_lines(lines, summary)
        self.assertIn("Follow-up recommendation:", lines)
        self.assertIn("- action: integrate_then_next", lines)

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

    def test_run_status_summary_reports_evidence_counts_from_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = work_root / "runs" / "evidence-run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "backlog": {"items": []},
                        "inflight_attempts": [],
                        "steps": [
                            {
                                "task_id": "TASK-001",
                                "result": {
                                    "evidence_status": "complete",
                                    "evidence_level": "L1",
                                },
                            },
                            {
                                "task_id": "TASK-002",
                                "result": {
                                    "evidence_status": "incomplete",
                                    "evidence_level": "L2",
                                },
                            },
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_run_status_summary(profile, run_dir)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                _write_status_text(summary)

            self.assertEqual(
                summary["evidence"],
                {"complete": 1, "incomplete": 1, "blocked": 0, "escalated": 0},
            )
            self.assertIn(
                "evidence: complete=1, incomplete=1",
                stdout.getvalue(),
            )

    def test_run_status_summary_preserves_token_usage_source_and_unavailable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            reported_run_dir = work_root / "runs" / "reported-run"
            reported_state_dir = reported_run_dir / "state"
            reported_state_dir.mkdir(parents=True)
            _write_json(
                reported_state_dir / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "backlog": {"items": []},
                    "inflight_attempts": [],
                    "steps": [
                        {
                            "task_id": "TASK-001",
                            "result": {
                                "token_usage": {
                                    "usage_source": "codex_jsonl",
                                    "input_tokens": 1200,
                                    "output_tokens": 300,
                                    "total_tokens": 1500,
                                },
                            },
                        }
                    ],
                },
            )
            missing_run_dir = work_root / "runs" / "missing-run"
            missing_state_dir = missing_run_dir / "state"
            missing_state_dir.mkdir(parents=True)
            _write_json(
                missing_state_dir / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "backlog": {"items": []},
                    "inflight_attempts": [],
                    "steps": [{"task_id": "TASK-002", "result": {}}],
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            reported = _build_run_status_summary(profile, reported_run_dir)
            missing = _build_run_status_summary(profile, missing_run_dir)

            self.assertEqual(reported["token_usage"]["usage_status"], "reported")
            self.assertEqual(reported["token_usage"]["usage_source"], "codex_jsonl")
            self.assertEqual(reported["token_usage"]["usage_sources"], ["codex_jsonl"])
            self.assertEqual(
                missing["token_usage"]["unavailable_reason"],
                "missing_runtime_token_usage",
            )

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

    def test_execution_result_text_reports_repo_map_handoff_reuse(self):
        result = {
            "status": "completed",
            "taskpack_id": "follow-up-run",
            "follow_up": {"source_taskpack_id": "previous-run"},
            "repo_map_handoff_reuse": {
                "status": "applied",
                "handoff_path": taskpack_module.REPO_MAP_HANDOFF_PATH,
                "removed_task_count": 1,
            },
            "report": {
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {},
            },
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn(
            "repo_map_handoff_reuse: status=applied; path=.agentteam/generated/repo_map_handoff.json",
            output,
        )
        self.assertIn("removed_repo_map_tasks=1", output)

    def test_execution_result_text_aggregates_multiple_task_summary_fields(self):
        result = {
            "status": "completed",
            "taskpack_id": "multi-task-run",
            "report": {
                "report_path": "/tmp/multi-task-report.md",
                "run_status": "completed",
                "task_count": 2,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["实现任务 A 的代码路径。", "补充任务 B 的验证入口。"],
                    "changed_files": ["src/a.py", "src/b.py"],
                    "verification": ["unit-a: passed", "unit-b: passed"],
                    "integration": "passed",
                    "integration_recommendation": "Run `agentteam integrate --taskpack multi-task-run`.",
                    "next_steps": ["继续验证 A。", "继续验证 B。"],
                    "evidence_gaps": [],
                },
            },
            "paths": {"run_dir": "/tmp/multi-task-run"},
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn(
            "work_report: changed=实现任务 A 的代码路径。；补充任务 B 的验证入口。",
            output,
        )
        self.assertIn("files=src/a.py；src/b.py", output)
        self.assertIn("verification=unit-a: passed；unit-b: passed", output)
        self.assertIn("next=继续验证 A。；继续验证 B。", output)

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

    def test_agentteam_cli_report_json_includes_fresh_projection_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "report-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "report-db-project")
            _write_completed_operator_run(run_dir)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "report-db-run"})
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(
                summary["projection_run"]["report_path"],
                str((run_dir / "reports" / "final_report.json").resolve()),
            )

    def test_agentteam_cli_report_json_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "report-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "report-db-project")
            _write_completed_operator_run(run_dir)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "report-db-run"})
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertIsNone(summary["projection_run"])

            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("projection_source: files", text_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", text_completed.stdout)

    def test_agentteam_cli_projection_readthrough_matches_fresh_db_after_stale_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "projection-parity-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "projection-parity-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                run_dir / "reports" / "final_report.json",
                {"run_id": "projection-parity-run"},
            )
            _write_json(
                work_root / "frozen" / "projection-parity-run" / "taskpack.yaml",
                {
                    "taskpack_id": "projection-parity-run",
                    "goal": "Projection parity fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            rebuild_project_projection_db(work_root)

            expected_fresh = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for summary in expected_fresh.values():
                self.assertProjectionFreshMetadata(summary)
            expected = {
                "status": self._projection_parity_payload(
                    "status",
                    expected_fresh["status"],
                ),
                "logs": self._projection_parity_payload(
                    "logs",
                    expected_fresh["logs"],
                ),
                "report": self._projection_parity_payload(
                    "report",
                    expected_fresh["report"],
                ),
                "taskpack_list": self._projection_parity_payload(
                    "taskpack_list",
                    expected_fresh["taskpack_list"],
                ),
            }

            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )

            stale = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for command, summary in stale.items():
                if command == "status":
                    self.assertProjectionFallbackHealthMetadata(summary, "stale")
                    self.assertEqual(summary.get("projection_next_action"), "run agentteam db rebuild")
                    self.assertIn(
                        "agentteam report --taskpack projection-parity-run",
                        summary.get("next_action") or "",
                    )
                else:
                    self.assertProjectionFallbackMetadata(summary, "stale")
                self.assertEqual(
                    self._projection_parity_payload(command, summary),
                    expected[command],
                )

            rebuild_project_projection_db(work_root)
            (work_root / "agentteam.db").write_text("not sqlite", encoding="utf-8")
            corrupt = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for command, summary in corrupt.items():
                if command == "status":
                    self.assertProjectionFallbackHealthMetadata(summary, "corrupt")
                    self.assertEqual(summary.get("projection_next_action"), "run agentteam db rebuild")
                    self.assertIn(
                        "agentteam report --taskpack projection-parity-run",
                        summary.get("next_action") or "",
                    )
                else:
                    self.assertProjectionFallbackMetadata(summary, "corrupt")
                self.assertEqual(
                    self._projection_parity_payload(command, summary),
                    expected[command],
                )

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
            controller_roots = list(
                (
                    run_dir
                    / "state"
                    / "controller_invocations"
                    / "runtime_diagnostic"
                ).glob("DIAGNOSTIC-SESSION-*")
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
            self.assertEqual(claim["usage_stage"], "runtime_diagnostic")
            self.assertEqual(started["usage_stage"], "runtime_diagnostic")
            self.assertEqual(
                started["runtime_execution_session_id"],
                claim["runtime_execution_session_id"],
            )
            self.assertEqual(
                terminal["terminal_writer"],
                "runtime_diagnostic_controller",
            )

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
            self.assertIn("中文简报:", completed.stdout)
            self.assertIn("本次运行已完成，共 1 个任务，0 个阻塞。", completed.stdout)
            self.assertIn("Integration recommendation: Review the final report, then run `agentteam integrate --taskpack taskpack-7` from a clean target repository if these changes should land.", completed.stdout)
            self.assertIn("Review gate:", completed.stdout)
            self.assertIn("report_command: agentteam report --taskpack taskpack-7", completed.stdout)
            self.assertIn("paths_command: agentteam paths --taskpack taskpack-7", completed.stdout)
            self.assertIn("diff_command: git -C ", completed.stdout)
            self.assertIn("integrate_command: agentteam integrate --taskpack taskpack-7", completed.stdout)
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
                report_json["completion_summary"]["review_gate"]["status"],
                "review_gate_required",
            )
            self.assertEqual(
                report_json["completion_summary"]["review_gate"]["integrate_command"],
                "agentteam integrate --taskpack taskpack-7",
            )
            self.assertEqual(
                report_json["completion_summary"]["next_steps"],
                ["Run the full competition validation package."],
            )
            self.assertEqual(
                report_json["completion_summary"]["chinese_operator_brief"][0],
                "本次运行已完成，共 1 个任务，0 个阻塞。",
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

    def test_model_invocation_usage_aggregation_keeps_exact_partial_and_open_states_separate(self):
        summary = aggregate_model_invocation_usage(
            _full_run_model_invocation_events()
        )

        self.assertEqual(summary["invocation_count"], 7)
        self.assertEqual(summary["terminal_invocation_count"], 6)
        self.assertEqual(summary["supported_invocation_count"], 6)
        self.assertEqual(
            summary["usage_status_counts"],
            {
                "reported": 2,
                "partial": 2,
                "unavailable": 1,
                "not_applicable": 1,
            },
        )
        self.assertEqual(summary["open_invocations"], 1)
        self.assertEqual(summary["open_supported_invocations"], 1)
        self.assertEqual(
            summary["lifecycle_terminal_coverage"],
            {
                "covered": 5,
                "total": 6,
                "percent": 83.33,
                "status": "partial",
            },
        )
        self.assertEqual(
            summary["token_usage_coverage"],
            {
                "covered": 2,
                "total": 6,
                "percent": 33.33,
                "status": "partial",
            },
        )
        self.assertEqual(summary["reported_totals_scope"], "reported_subset")
        self.assertEqual(
            summary["reported_token_totals"],
            {
                "input_tokens": 150,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_tokens": 3,
                "total_tokens": 180,
                "contributing_invocation_count": 2,
            },
        )
        self.assertEqual(
            summary["partial_known_token_lower_bounds"],
            {
                "input_tokens": 7,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 9,
                "contributing_invocation_count": 1,
            },
        )
        self.assertEqual(
            summary["observed_token_lower_bound"]["total_tokens"],
            189,
        )
        self.assertEqual(
            summary["stage_breakdown"]["implementation_worker"][
                "invocation_count"
            ],
            3,
        )
        self.assertEqual(
            summary["stage_breakdown"]["runtime_diagnostic"][
                "invocation_count"
            ],
            1,
        )
        self.assertEqual(
            summary["stage_breakdown"]["development_smoke"][
                "invocation_count"
            ],
            1,
        )
        self.assertEqual(
            summary["reason_counts"]["partial"],
            [
                {"reason": "provider_payload_incomplete", "count": 1},
                {
                    "reason": "provider_session_lineage_ambiguous",
                    "count": 1,
                },
            ],
        )
        self.assertEqual(
            summary["reason_counts"]["unavailable"],
            [{"reason": "timeout_before_usage", "count": 1}],
        )
        self.assertEqual(
            summary["completion_status"],
            "blocked_open_invocations",
        )
        self.assertFalse(summary["benchmark_ready"])

    def test_legacy_only_report_remains_readable_and_is_not_benchmark_counted(self):
        from agentteam_runtime.operator_report import (
            build_run_completion_report,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_completed_operator_run(
                Path(tmp) / "runs" / "legacy-only"
            )
            report = build_run_completion_report(
                run_dir,
                project="legacy-project",
                write_files=False,
            )
            markdown = render_run_completion_report(report)

        self.assertIsNone(report["model_invocation_usage"])
        self.assertEqual(report["token_usage"]["total_tokens"], 1500)
        self.assertEqual(
            report["legacy_task_token_usage"],
            {
                "accounting_scope": "legacy_task_results",
                "benchmark_counted": False,
                "usage": report["token_usage"],
            },
        )
        self.assertIn(
            "Token usage: total=1500 input=1200 output=300 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            markdown,
        )

    def test_terminal_and_feishu_reports_share_canonical_usage_and_label_legacy_totals(self):
        from agentteam_runtime.operator_report import (
            build_run_completion_report,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "full-usage"
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {"scheduler_status": "idle", "steps": []},
            )
            operator_report = {
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [],
                "token_usage": {
                    "usage_status": "reported",
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 900,
                    "output_tokens": 99,
                    "total_tokens": 999,
                },
            }
            terminal_event = {
                "event_id": "EVT-RUN-COMPLETED",
                "event_type": "run_completed",
                "sequence": 100,
                "payload": {
                    "run_status": "completed",
                    "scheduler_status": "idle",
                    "operator_report": operator_report,
                },
            }
            _write_jsonl(
                run_dir / "events.jsonl",
                [*_full_run_model_invocation_events(), terminal_event],
            )

            report = build_run_completion_report(
                run_dir,
                project="usage-project",
                write_files=False,
            )
            markdown = render_run_completion_report(report)
            sent_payloads = []

            def fake_http_post(_url, payload, _timeout_seconds):
                sent_payloads.append(payload)
                return {"status_code": 200, "body": {"code": 0}}

            notifier = FeishuWebhookNotifier(
                webhook_url="https://example.invalid/hook",
                project="usage-project",
                http_post=fake_http_post,
                message_limit=4000,
            )
            notification = notifier.notify_event(
                terminal_event,
                run_dir=str(run_dir),
            )

        self.assertEqual(report["task_count"], 1)
        self.assertEqual(
            report["model_invocation_usage"]["invocation_count"],
            7,
        )
        self.assertEqual(
            report["model_invocation_usage"]["reported_token_totals"][
                "total_tokens"
            ],
            180,
        )
        self.assertEqual(
            report["legacy_task_token_usage"]["usage"]["total_tokens"],
            999,
        )
        self.assertFalse(
            report["legacy_task_token_usage"]["benchmark_counted"]
        )
        self.assertEqual(report["run_outcome"], "blocked_open_invocations")
        self.assertEqual(notification["event_type"], "notification_sent")
        feishu_text = sent_payloads[0]["content"]["text"]
        for line in compact_model_invocation_usage_lines(
            report["model_invocation_usage"]
        ):
            self.assertIn(line, markdown)
            self.assertIn(line, feishu_text)
        self.assertIn(
            "Token usage: total=999 input=900 output=99 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            markdown,
        )
        self.assertIn(
            "Token usage: total=999 input=900 output=99 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            feishu_text,
        )
        self.assertNotIn("total=6999", markdown)

    def test_concise_report_lines_include_chinese_operator_brief(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "chinese_operator_brief": [
                        "本次运行已完成，共 1 个任务，0 个阻塞。",
                        "主要变更：Optimized the gesture scoring pipeline.",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("中文简报: 本次运行已完成，共 1 个任务，0 个阻塞。", lines)
        self.assertIn("中文简报: 主要变更：Optimized the gesture scoring pipeline.", lines)

    def test_concise_report_lines_include_chinese_operator_digest(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "operator_digest": [
                        "做了什么：优化了手势评分流水线的数据窗口复制。",
                        "验证结果：unit_tests: passed",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("中文工作汇报: 做了什么：优化了手势评分流水线的数据窗口复制。", lines)
        self.assertIn("中文工作汇报: 验证结果：unit_tests: passed", lines)

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

    def test_concise_report_lines_include_agentteam_target_review_gate(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["已实现 AgentTeam 目标仓库策略。"],
                    "integration": "passed",
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

        self.assertIn(
            "agentteam_target_review: source merge, push, and release activation require operator review",
            lines,
        )

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
            installed_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertIn(
                f"Installed launcher sha256: {installed_digest}",
                completed.stdout,
            )
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

    def test_agentteam_cli_init_infers_native_runtime_verification_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            runtime_root = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            (runtime_root / "agentteam_runtime").mkdir(parents=True)
            (runtime_root / "agentteam_runtime" / "__init__.py").write_text("", encoding="utf-8")
            (runtime_root / "tests").mkdir()
            (runtime_root / "tests" / "test_taskpack.py").write_text("", encoding="utf-8")
            (runtime_root / "tests" / "test_m0_runtime.py").write_text("", encoding="utf-8")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "agentteam-native",
                    "--work-root",
                    str(work_root),
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
            self.assertEqual(
                profile["verification_profile"]["correctness"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )

    def test_submit_args_upgrade_legacy_native_runtime_default_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            runtime_root = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            (runtime_root / "agentteam_runtime").mkdir(parents=True)
            (runtime_root / "agentteam_runtime" / "__init__.py").write_text("", encoding="utf-8")
            (runtime_root / "tests").mkdir()
            (runtime_root / "tests" / "test_taskpack.py").write_text("", encoding="utf-8")
            (runtime_root / "tests" / "test_m0_runtime.py").write_text("", encoding="utf-8")
            args = SimpleNamespace(
                goal="Improve reports.",
                work_root=None,
                taskpack_id=None,
                author_runtime=None,
                runtime=None,
                codex_timeout_seconds=600,
                one_shot=None,
                max_inflight=None,
                max_attempts=None,
                commit_verified_integration=None,
                notification_project=None,
                feishu_webhook_env=None,
                feishu_signing_secret_env=None,
                codex_command=None,
            )
            profile = {
                "project_key": "agentteam-native",
                "work_root": str(tmp_path / "work"),
                "author_runtime": "codex",
                "default_runtime": "codex",
                "verification_profile": {
                    "verification_profile_schema_version": "agentteam_verification_profile.v1",
                    "correctness": {"command": ["python3", "-m", "unittest", "discover"]},
                    "performance": {"command": None, "metrics": []},
                },
            }

            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(
                submit_args.verification_profile["correctness"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )

    def test_submit_args_from_profile_propagates_worker_codex_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            args = SimpleNamespace(
                goal="Use a lower cost worker model.",
                work_root=None,
                taskpack_id=None,
                author_runtime=None,
                runtime=None,
                codex_timeout_seconds=600,
                codex_model=None,
                one_shot=None,
                max_inflight=None,
                max_attempts=None,
                commit_verified_integration=None,
                notification_project=None,
                feishu_webhook_env=None,
                feishu_signing_secret_env=None,
                codex_command=None,
            )
            profile = {
                "project_key": "model-profile",
                "work_root": str(tmp_path / "work"),
                "author_runtime": "codex",
                "default_runtime": "codex",
                "codex_model": "medium",
            }

            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(submit_args.codex_model, "medium")

            args.codex_model = "strong-review-model"
            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(submit_args.codex_model, "strong-review-model")

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

    def test_invocation_supervision_probe_proves_linger_identity_and_cleanup_without_provider(self):
        host = _InvocationProbeFakeHost()
        with mock.patch.object(
            agentteam_module,
            "draft_taskpack_from_goal",
            side_effect=AssertionError("provider path must not be called"),
        ) as author_call, mock.patch.object(
            agentteam_module,
            "run_runtime_diagnostic_chat",
            side_effect=AssertionError("provider path must not be called"),
        ) as chat_call:
            summary = agentteam_module._run_invocation_supervision_probe(
                timeout_seconds=1.0,
                host=host,
            )

        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["linger"])
        self.assertTrue(summary["enclosing_identity_stable"])
        self.assertTrue(summary["user_manager_identity_stable"])
        self.assertTrue(summary["unit_identity_verified"])
        self.assertTrue(summary["cgroup_drained"])
        self.assertTrue(summary["cleanup_complete"])
        self.assertEqual(summary["provider_calls"], 0)
        self.assertEqual(host.enclosing_show_count, 3)
        self.assertTrue(host.released)
        self.assertTrue(host.pidfd_closed)
        self.assertTrue(host.state_removed)
        author_call.assert_not_called()
        chat_call.assert_not_called()
        self.assertTrue(
            all(command[0] in {"loginctl", "systemctl", "systemd-run"} for command in host.commands)
        )
        systemd_run = next(command for command in host.commands if command[0] == "systemd-run")
        self.assertIn("--property=Type=oneshot", systemd_run)
        self.assertIn("--property=RemainAfterExit=yes", systemd_run)
        self.assertIn("--property=KillMode=control-group", systemd_run)
        self.assertIn("--no-block", systemd_run)
        self.assertTrue(
            any(command[:3] == ["systemctl", "--user", "stop"] for command in host.commands)
        )
        self.assertTrue(
            any(command[:3] == ["systemctl", "--user", "reset-failed"] for command in host.commands)
        )

    def test_invocation_supervision_probe_fails_closed_for_every_bounded_branch(self):
        cases = {
            "non_linux": "linux_required",
            "pidfd_unavailable": "pidfd_open_unavailable",
            "boot_id_missing": "boot_id_missing",
            "command_missing": "required_command_unavailable",
            "command_timeout": "timeout",
            "loginctl_failed": "loginctl_failed",
            "linger_disabled": "linger_disabled",
            "enclosing_unavailable": "enclosing_user_service_unavailable",
            "enclosing_identity_missing": "enclosing_identity_missing",
            "kill_mode_unsuitable": "enclosing_kill_mode_unsuitable",
            "manager_identity_missing": "user_manager_identity_missing",
            "process_identity_missing": "process_identity_missing",
            "unexpected_unit_reuse": "unexpected_unit_reuse",
            "transient_start_failed": "transient_unit_start_failed",
            "unit_identity_missing": "unit_identity_missing",
            "unit_property_mismatch": "unit_property_mismatch",
            "helper_cgroup_mismatch": "helper_cgroup_mismatch",
            "enclosing_restarted": "enclosing_user_service_restarted",
            "manager_identity_changed": "user_manager_identity_changed",
            "unit_show_failed": "unit_identity_unavailable",
            "unit_identity_changed": "unit_identity_changed",
            "helper_identity_changed": "helper_identity_changed",
            "pidfd_open_failed": "pidfd_open_failed",
            "release_failed": "host_operation_failed",
            "helper_exit_timeout": "helper_exit_timeout",
            "cgroup_events_unavailable": "cgroup_events_unavailable",
            "cgroup_events_invalid": "cgroup_events_invalid",
            "cgroup_drain_timeout": "timeout",
            "unit_identity_not_queryable": "unit_identity_not_queryable",
            "cleanup_incomplete": "cleanup_incomplete",
        }
        for scenario, expected_failure in cases.items():
            with self.subTest(scenario=scenario):
                host = _InvocationProbeFakeHost(scenario)
                summary = agentteam_module._run_invocation_supervision_probe(
                    timeout_seconds=0.08,
                    host=host,
                )
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["failure_code"], expected_failure)
                self.assertEqual(summary["provider_calls"], 0)
                if host.unit_started:
                    self.assertTrue(
                        any(
                            command[:3] == ["systemctl", "--user", "stop"]
                            for command in host.commands
                        )
                    )
                    self.assertTrue(host.state_removed)

    def test_invocation_supervision_probe_json_is_compact_and_secret_free(self):
        summary = agentteam_module._run_invocation_supervision_probe(
            timeout_seconds=1.0,
            host=_InvocationProbeFakeHost(),
        )
        expected_fields = {
            "probe",
            "status",
            "linux",
            "pidfd_open",
            "linger",
            "boot_id",
            "enclosing_kill_mode",
            "enclosing_identity_stable",
            "user_manager_identity_stable",
            "transient_unit_created",
            "unit_identity_verified",
            "pidfd_opened",
            "cgroup_drained",
            "unit_identity_queryable_after_exit",
            "cleanup_complete",
            "provider_calls",
            "failure_code",
        }
        self.assertEqual(set(summary), expected_fields)
        with mock.patch.dict(os.environ, {"AGENTTEAM_TEST_SECRET": "do-not-leak"}):
            with mock.patch.object(
                agentteam_module,
                "_run_invocation_supervision_probe",
                return_value=summary,
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = agentteam_module.main(
                        ["doctor", "--invocation-supervision-probe", "--json"]
                    )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload), expected_fields)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("/tmp/", serialized)

        failed_summary = dict(summary, status="failed", failure_code="linger_disabled")
        with mock.patch.object(
            agentteam_module,
            "_run_invocation_supervision_probe",
            return_value=failed_summary,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agentteam_module.main(
                    ["doctor", "--invocation-supervision-probe", "--json"]
                )
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["failure_code"], "linger_disabled")

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

    def test_agentteam_cli_gc_dry_run_explains_projected_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "projection-run"})
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
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
            artifact_projection = summary["artifact_projection"]
            self.assertEqual(artifact_projection["projection_source"], "db")
            self.assertEqual(artifact_projection["check_status"], "passed")
            self.assertGreaterEqual(artifact_projection["total_artifacts"], 3)
            self.assertGreater(artifact_projection["total_bytes"], 0)
            self.assertGreaterEqual(
                artifact_projection["retention_policies"]["authoritative"],
                1,
            )
            self.assertGreaterEqual(
                artifact_projection["retention_policies"]["rebuildable"],
                1,
            )
            self.assertIn("report", artifact_projection["artifact_types"])
            self.assertIn("repo_context", artifact_projection["artifact_types"])
            explanation_policies = {
                item["retention_policy"]
                for item in artifact_projection["dry_run_explanations"]
            }
            self.assertIn("authoritative", explanation_policies)
            self.assertIn("rebuildable", explanation_policies)

    def test_agentteam_cli_gc_artifacts_json_lists_rebuildable_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--artifact-limit",
                    "1",
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
            plan = summary["artifact_retention_plan"]
            self.assertEqual(plan["projection_source"], "db")
            self.assertEqual(plan["plan_status"], "ready")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual(len(plan["candidates"]), 1)
            self.assertEqual(plan["candidates"][0]["retention_policy"], "rebuildable")

    def test_agentteam_cli_gc_artifacts_json_reports_unavailable_without_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
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
            self.assertProjectionFallbackMetadata(summary["artifact_projection"], "missing")
            plan = summary["artifact_retention_plan"]
            self.assertProjectionFallbackMetadata(plan, "missing")
            self.assertEqual(plan["plan_status"], "unavailable")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 0)

            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("artifact_projection: files", text_completed.stdout)
            self.assertIn("artifact_projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("artifact_projection_next_action: run agentteam db rebuild", text_completed.stdout)
            self.assertIn("artifact_retention_plan_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("artifact_retention_plan_next_action: run agentteam db rebuild", text_completed.stdout)

    def test_agentteam_cli_gc_delete_artifacts_deletes_only_validated_rebuildable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            report_path = run_dir / "reports" / "final_report.json"
            event_path = run_dir / "events.jsonl"
            repo_context = run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            role_context = run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            taskpack_path = work_root / "frozen" / "retention-run" / "taskpack.json"
            _write_json(report_path, {"run_id": "retention-run"})
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1"})
            _write_json(role_context, {"context_schema_version": "role_context.v1"})
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention delete fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--delete-artifacts",
                    "--artifact-limit",
                    "10",
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
            deletion = summary["artifact_retention_plan"]["artifact_deletion"]
            self.assertEqual(deletion["deletion_status"], "completed")
            self.assertEqual(deletion["deleted_count"], 2)
            self.assertFalse(repo_context.exists())
            self.assertFalse(role_context.exists())
            self.assertTrue(report_path.exists())
            self.assertTrue(event_path.exists())
            self.assertTrue(taskpack_path.exists())
            self.assertEqual(summary["artifact_retention_plan"]["next_action"], "run agentteam db rebuild")

    def test_agentteam_cli_gc_delete_artifacts_blocks_when_candidate_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            repo_context = run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1"})
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention delete fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1", "changed": True})

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--delete-artifacts",
                    "--force",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            error = json.loads(completed.stdout or completed.stderr)
            self.assertEqual(error["error"], "artifact deletion blocked")
            self.assertEqual(error["artifact_deletion_status"], "blocked")
            self.assertTrue(repo_context.exists())

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
            self.assertIn("工作摘要:", message)
            self.assertIn("中文工作汇报:", message)
            self.assertIn(
                "Token usage: not applicable (diagnostic notification; no AgentTeam run)",
                message,
            )
            self.assertNotIn("Token usage: unavailable", message)
            self.assertIn("AgentTeam notification test for notify-project.", message)
            self.assertIn("If you receive this message, Feishu notification delivery works.", message)
            self.assertNotIn("Completion summary:", message)
            self.assertNotIn("中文简报:", message)
            self.assertNotIn("What changed:", message)

    def test_agentteam_cli_notify_run_completed_sends_existing_run_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "completed-run"
            _init_repo(repo)
            _write_completed_operator_run(run_dir)
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "worker_count": 1,
                        "pool_diagnostic_status": "attention",
                        "diagnostic_worker_counts": {"processing_stale": 1},
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "running",
                                "worker_diagnostic_state": "processing_stale",
                                "last_activity": "processing",
                                "heartbeat_age_seconds": 181,
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
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
            self.assertIn("工作摘要:", message)
            self.assertIn("中文工作汇报:", message)
            self.assertIn("Token usage: total=1500 input=1200 output=300 reported=1/1", message)
            self.assertIn("Scanned the repository and implemented one evidence-backed optimization.", message)
            self.assertIn("gesture_recognition/sim_eval.py", message)
            self.assertIn("Worker 诊断：pool=attention；processing_stale=1", message)
            self.assertIn("implementation-worker-1 diagnostic=processing_stale", message)
            self.assertNotIn("Completion summary:", message)
            self.assertNotIn("What changed:", message)

    def test_agentteam_cli_notify_diagnose_dry_run_does_not_print_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            env = _test_env()
            env["AGENTTEAM_FEISHU_DIAG_WEBHOOK"] = (
                "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
            )
            env["AGENTTEAM_FEISHU_DIAG_SECRET"] = "demo-secret"
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "notify-diagnose-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--notification-project",
                    "notify-diagnose-project",
                    "--feishu-webhook-env",
                    "AGENTTEAM_FEISHU_DIAG_WEBHOOK",
                    "--feishu-signing-secret-env",
                    "AGENTTEAM_FEISHU_DIAG_SECRET",
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
                    "diagnose",
                    "--project-root",
                    str(repo),
                    "--dry-run",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("secret-token", completed.stdout)
            self.assertNotIn("demo-secret", completed.stdout)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["diagnosis_status"], "dry_run")
            self.assertEqual(summary["provider"], "feishu")
            self.assertEqual(summary["project"], "notify-diagnose-project")
            self.assertEqual(summary["webhook_env"], "AGENTTEAM_FEISHU_DIAG_WEBHOOK")
            self.assertTrue(summary["webhook_env_set"])
            self.assertEqual(summary["signing_secret_env"], "AGENTTEAM_FEISHU_DIAG_SECRET")
            self.assertTrue(summary["signing_enabled"])
            self.assertEqual(
                [item["variant"] for item in summary["variants"]],
                ["rich_text", "concise_text"],
            )
            self.assertEqual(
                [item["status"] for item in summary["variants"]],
                ["dry_run", "dry_run"],
            )

    def test_permission_request_notification_includes_approve_and_deny_hints(self):
        message = _permission_request_text(
            {
                "payload": {
                    "task_id": "optimize-pipeline",
                    "request_id": "PERM-001",
                    "requested_capability": "sandbox_escalation",
                    "reason": "network is restricted",
                }
            },
            "/tmp/agentteam-run",
            "notify-project",
        )

        self.assertIn("[AgentTeam] permission request required", message)
        self.assertIn("Capability: sandbox_escalation", message)
        self.assertIn(
            "Approve: agentteam permissions approve --run-dir /tmp/agentteam-run --request-id PERM-001",
            message,
        )
        self.assertIn(
            "Deny: agentteam permissions deny --run-dir /tmp/agentteam-run --request-id PERM-001",
            message,
        )

    def test_feishu_notification_failure_summary_includes_body_message(self):
        def fake_http_post(_url, _payload, _timeout_seconds):
            return {
                "status_code": 200,
                "body": {
                    "code": 11232,
                    "msg": "security keyword mismatch",
                },
            }

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/redacted-token",
            project="notify-project",
            http_post=fake_http_post,
        )
        result = notifier.notify_event(
            {
                "event_id": "EVT-001",
                "sequence": 1,
                "event_type": "run_completed",
                "payload": {"run_status": "completed"},
            },
            run_dir="/tmp/run",
        )

        payload = result["payload"]
        self.assertEqual(payload["notification_status"], "failed")
        self.assertIn("body_code=11232", payload["error_summary"])
        self.assertIn("body_msg=security keyword mismatch", payload["error_summary"])
        self.assertNotIn("redacted-token", payload["error_summary"])

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
            diff_base = (
                summary["integration_baseline"].get("base_sha")
                or summary["integration_baseline"]["head_sha"]
            )
            diff_head = (
                summary["integration_baseline"]["head_sha"]
                if summary["integration_baseline"].get("base_sha")
                else "HEAD"
            )
            expected_review_diff = (
                f"git -C {baseline_worktree.resolve()} diff --stat {diff_base}..{diff_head}"
            )
            self.assertEqual(
                summary["review_commands"],
                {
                    "report": "agentteam report --taskpack paths-run",
                    "paths": "agentteam paths --taskpack paths-run",
                    "diff": expected_review_diff,
                    "integrate": "agentteam integrate --taskpack paths-run",
                },
            )
            self.assertEqual(
                summary["read_only_review_commands"],
                {
                    "report": "agentteam report --taskpack paths-run",
                    "paths": "agentteam paths --taskpack paths-run",
                    "diff": expected_review_diff,
                },
            )
            self.assertEqual(
                summary["accept_command"],
                "agentteam integrate --taskpack paths-run",
            )
            self.assertIn("review_report: agentteam report --taskpack paths-run\n", text_completed.stdout)
            self.assertIn(
                f"review_diff: {expected_review_diff}\n",
                text_completed.stdout,
            )
            self.assertIn("review_integrate: agentteam integrate --taskpack paths-run\n", text_completed.stdout)
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

    def test_agentteam_cli_integrate_record_only_marks_baseline_acknowledged_without_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "record-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for record-only integration.",
                "record-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "record-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nmanual-only\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam record-only fixture"],
                cwd=baseline_worktree,
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
                    "record-integrate-run",
                    "--record-only",
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
            self.assertEqual(summary["integrate_status"], "acknowledged")
            self.assertEqual(summary["merge_status"], "record_only")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n")
            state = json.loads(
                (
                    work_root
                    / "runs"
                    / "record-integrate-run"
                    / "state"
                    / "two_phase_scheduler_state.json"
                ).read_text(encoding="utf-8")
            )
            baseline = state["integration_baseline"]
            self.assertEqual(baseline["integration_baseline_status"], "acknowledged")
            self.assertEqual(baseline["integration_acknowledged_by"], "operator")
            self.assertTrue(baseline["integration_acknowledged_head_sha"])

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(work_root / "runs" / "record-integrate-run"),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            status = json.loads(status_completed.stdout)
            self.assertEqual(status["integration_baseline"]["status"], "acknowledged")
            self.assertNotIn("agentteam integrate", status.get("next_action") or "")
            self.assertNotIn("review integration baseline", status.get("next_action") or "")

    def test_status_integration_counts_use_latest_attempt_per_task(self):
        snapshot = {
            "integration_queue": {
                "task-1:attempt-1": {
                    "task_id": "task-1",
                    "attempt_id": "attempt-1",
                    "queue_status": "blocked",
                },
                "task-1:attempt-2": {
                    "task_id": "task-1",
                    "attempt_id": "attempt-2",
                    "queue_status": "committed",
                },
                "task-2:attempt-1": {
                    "task_id": "task-2",
                    "attempt_id": "attempt-1",
                    "queue_status": "verified",
                },
            }
        }

        self.assertEqual(
            agentteam_module._status_integration_counts(snapshot),
            {"total": 2, "blocked": 0, "verified": 2},
        )

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

    def test_post_backlog_context_maps_versioned_run_to_versioned_frozen_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "work"
            project_root = root / "repo"
            run_dir = work_root / "runs" / "v4" / "phase1-run"
            run_dir.mkdir(parents=True)
            project_root.mkdir()
            versioned = {
                "taskpack_id": "phase1-run",
                "project_root": str(project_root),
                "post_backlog_gates": [{"gate_id": "P1-LIVE"}],
                "marker": "versioned",
            }
            flat = {**versioned, "marker": "flat"}
            _write_json(
                work_root / "frozen" / "v4" / "phase1-run" / "taskpack.yaml",
                versioned,
            )
            _write_json(
                work_root / "frozen" / "phase1-run" / "taskpack.yaml",
                flat,
            )

            context = agentteam_module._post_backlog_gate_context(
                {"work_root": str(work_root)},
                run_dir,
            )

            self.assertEqual(context["taskpack"]["marker"], "versioned")
            self.assertEqual(
                context["frozen_dir"],
                (work_root / "frozen" / "v4" / "phase1-run").resolve(),
            )

    def test_post_backlog_repair_publishes_new_epoch_with_candidate_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            run_dir = root / "work" / "runs" / "v4" / "phase1-run"
            current_worktree = root / "current"
            repair_worktree = root / "repair"
            for path in (repo, run_dir, current_worktree, repair_worktree):
                path.mkdir(parents=True)
            runtime_package = (
                repair_worktree
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
            )
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            current_head = "a" * 40
            target_head = "b" * 40
            repair_head = "c" * 40
            context = {
                "project_root": repo.resolve(),
                "work_root": root / "work",
                "run_dir": run_dir,
                "declarations_by_id": {"P1-LIVE": {}, "P1-06E": {}},
                "gate_root": run_dir / "state" / "post_backlog_gates",
                "epochs_root": (
                    run_dir / "state" / "post_backlog_gates" / "epochs"
                ),
                "locks_root": (
                    run_dir / "state" / "post_backlog_gates" / "locks"
                ),
            }
            record = {
                "schema_version": "post_backlog_gate_epoch.v1",
                "implementation_run_id": "phase1-run",
                "epoch_number": 1,
                "prior_epoch_sha256": None,
                "gate_declaration_sha256": "d" * 64,
                "git_object_format": "sha1",
                "target_branch": "target",
                "target_head_sha": target_head,
                "integration_branch": "integration-epoch-1",
                "integration_head_sha": current_head,
                "validated_code_sha": current_head,
                "verification_command_sha256": "e" * 64,
                "verification_result_sha256": "f" * 64,
                "created_at": "2026-01-01T00:00:00Z",
            }
            current = {"record": record, "digest": "1" * 64}
            calls = []

            def git_stdout(repo_path, command):
                if command == ["rev-parse", "--show-object-format"]:
                    return "sha1"
                if command == [
                    "rev-parse",
                    "--verify",
                    "integration-epoch-1^{commit}",
                ]:
                    return current_head
                if command == ["rev-parse", "--verify", "target^{commit}"]:
                    return target_head
                if command == [
                    "rev-parse",
                    "--verify",
                    "repair-branch^{commit}",
                ]:
                    return repair_head
                if command == ["status", "--porcelain=v1", "--untracked-files=all"]:
                    return ""
                if command == ["rev-parse", "HEAD"]:
                    self.assertEqual(Path(repo_path), repair_worktree)
                    return repair_head
                raise AssertionError((repo_path, command))

            def publish_epoch(_context, value):
                calls.append(value)
                path = context["epochs_root"] / str(value["epoch_number"])
                path.mkdir(parents=True)
                return path

            verification = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            verified_idle = {
                "status": "awaiting_post_backlog_gates",
            }
            with (
                mock.patch.object(
                    agentteam_module,
                    "_require_post_backlog_gate_context",
                    return_value=context,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_gate_mutation_locks",
                    return_value=nullcontext(),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_require_current_gate_epoch",
                    return_value=current,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_build_run_status_summary",
                    return_value=verified_idle,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_open_gate_controller_invocations",
                    return_value=[],
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_stdout",
                    side_effect=git_stdout,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_completed",
                    return_value=SimpleNamespace(returncode=0),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_worktree_for_branch",
                    side_effect=lambda _repo, branch, _head: (
                        repair_worktree
                        if branch == "repair-branch"
                        else current_worktree
                    ),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_frozen_gate_verification_command",
                    return_value=["python3", "-c", "pass"],
                ),
                mock.patch.object(
                    agentteam_module,
                    "_validate_gate_record_schema",
                ),
                mock.patch.object(
                    agentteam_module,
                    "_publish_gate_epoch",
                    side_effect=publish_epoch,
                ),
                mock.patch.object(
                    agentteam_module.subprocess,
                    "run",
                    return_value=verification,
                ) as run_mock,
            ):
                os.environ["AGENTTEAM_LAUNCHER_SELECTION"] = "outer-release"
                try:
                    summary = agentteam_module._gate_repair_baseline(
                        repo,
                        {},
                        run_dir,
                        expected_gate_epoch=1,
                        repair_branch="repair-branch",
                        expected_repair_head=repair_head,
                    )
                finally:
                    os.environ.pop("AGENTTEAM_LAUNCHER_SELECTION", None)

            self.assertEqual(summary["gate_epoch"], 2)
            self.assertEqual(summary["validated_code_sha"], repair_head)
            self.assertEqual(calls[0]["prior_epoch_sha256"], current["digest"])
            self.assertEqual(calls[0]["integration_branch"], "repair-branch")
            verification_env = run_mock.call_args.kwargs["env"]
            self.assertNotIn("AGENTTEAM_LAUNCHER_SELECTION", verification_env)
            self.assertTrue(
                verification_env["PYTHONPATH"].startswith(
                    str(
                        repair_worktree
                        / "experiments"
                        / "native_agentteam_runtime"
                        / "m0_runtime"
                    )
                )
            )

    def test_post_backlog_gate_git_oid_format_and_operator_tty_fail_closed(self):
        self.assertTrue(agentteam_module._valid_git_oid("a" * 40, "sha1"))
        self.assertFalse(agentteam_module._valid_git_oid("a" * 64, "sha1"))
        self.assertTrue(agentteam_module._valid_git_oid("b" * 64, "sha256"))
        self.assertFalse(agentteam_module._valid_git_oid("b" * 40, "sha256"))
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaises(agentteam_module.AgentTeamCliError):
                agentteam_module._require_operator_approval_context()

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
            self.assertIn("tasks: 2 done, 0 blocked", status_completed.stdout)
            self.assertIn("inflight: 0", status_completed.stdout)
            self.assertIn("manual_gates: 0", status_completed.stdout)
            self.assertIn("projection_source: files", status_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", status_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", status_completed.stdout)
            self.assertIn(str((work_root / "runs" / "cli-status-run").resolve()), status_completed.stdout)

    def test_agentteam_cli_status_can_replay_fresh_projection_db_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-db-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["latest_run"], "status-db-run")
            self.assertEqual(summary["tasks"]["done"], 1)
            self.assertEqual(summary["tasks"]["blocked"], 0)

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

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("projection_source: db", text_completed.stdout)
            self.assertIn("projection_status: fresh", text_completed.stdout)

    def test_agentteam_cli_status_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-db-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertProjectionFallbackHealthMetadata(summary, "stale")
            self.assertEqual(summary["projection_next_action"], "run agentteam db rebuild")
            self.assertIn("agentteam report --taskpack status-db-run", summary["next_action"])
            self.assertIn("agentteam paths --taskpack status-db-run", summary["next_action"])
            self.assertEqual(summary["latest_run"], "status-db-run")

    def test_agentteam_cli_status_prioritizes_integration_guidance_over_projection_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-actionable-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-actionable-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["projection_warning"], "projection_db_unavailable")
            self.assertIn("projection_next_action", summary)
            self.assertEqual(summary["projection_next_action"], "run agentteam db rebuild")
            self.assertIn("agentteam report --taskpack status-actionable-run", summary["next_action"])
            self.assertIn("agentteam paths --taskpack status-actionable-run", summary["next_action"])
            self.assertIn("integration baseline", summary["operator_hint"])
            self.assertNotEqual(summary["next_action"], summary["projection_next_action"])

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

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("projection_next_action: run agentteam db rebuild", text_completed.stdout)
            self.assertIn(
                "next_action: agentteam report --taskpack status-actionable-run",
                text_completed.stdout,
            )

    def test_agentteam_cli_status_prioritizes_running_guidance_before_integration_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-running-baseline"
            baseline_worktree = run_dir / "integration-baseline"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-running-project")
            baseline_worktree.mkdir(parents=True)
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "running",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/status-running-baseline/integration",
                        "integration_baseline_worktree_path": str(baseline_worktree.resolve()),
                        "integration_baseline_head_sha": "abc123",
                    },
                    "inflight_attempts": [
                        {
                            "task_id": "optimize-pipeline",
                            "attempt_id": "ATTEMPT-001",
                            "agent_id": "implementation-worker-1",
                        }
                    ],
                },
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["run_status"], "running")
            self.assertIn("agentteam watch --taskpack status-running-baseline", summary["next_action"])
            self.assertNotIn("review integration baseline", summary["next_action"])
            self.assertNotIn("agentteam integrate", summary["next_action"])
            self.assertIn("run is still active", summary["operator_hint"])

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
            self.assertIn("projection_source: files", completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", completed.stdout)
            self.assertIn("EVT-0001 run_completed", completed.stdout)

    def test_agentteam_cli_logs_can_read_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)

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
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["events"][0]["event_type"], "run_completed")

    def test_agentteam_cli_logs_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

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
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["events"][0]["event_type"], "backlog_updated")

    def test_agentteam_cli_logs_falls_back_when_projection_db_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            work_root.mkdir(parents=True, exist_ok=True)
            (work_root / "agentteam.db").write_text("not sqlite", encoding="utf-8")

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
            self.assertProjectionFallbackMetadata(summary, "corrupt")
            self.assertEqual(summary["event_count"], 1)

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

    def test_agentteam_cli_status_treats_stopped_stale_inflight_as_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stopped-stale-inflight-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "stopped",
                    "previous_scheduler_status": "running",
                    "stop_mode": "stale_cleanup",
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
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "stopped",
                    "stop_mode": "stale_cleanup",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-1",
                            "worker_status": "stopped",
                            "stopped_by": "stale_pid",
                        }
                    ],
                },
            )

            json_completed = subprocess.run(
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
            text_completed = subprocess.run(
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

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            self.assertEqual(summary["overall_status"], "stopped")
            self.assertEqual(summary["run_status"], "stopped")
            self.assertEqual(summary["liveness_status"], "stopped")
            self.assertEqual(summary["inflight"]["total"], 0)
            self.assertEqual(summary["inactive_inflight"]["total"], 1)
            self.assertNotIn("agentteam watch", summary.get("next_action", ""))
            self.assertNotIn("run is still active", summary.get("operator_hint", ""))

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("overall_status: stopped", text_completed.stdout)
            self.assertIn("run_status: stopped", text_completed.stdout)
            self.assertIn("liveness: stopped", text_completed.stdout)
            self.assertIn("inflight: 0", text_completed.stdout)
            self.assertIn("inactive_inflight: 1", text_completed.stdout)
            self.assertNotIn("agentteam watch", text_completed.stdout)
            self.assertNotIn("run is still active", text_completed.stdout)

    def test_agentteam_cli_status_treats_stop_requested_inflight_as_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stop-requested-inflight-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "stop_requested",
                    "previous_scheduler_status": "waiting",
                    "stop_mode": "stop",
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
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "stop_requested",
                    "stop_mode": "stop",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-1",
                            "worker_status": "stop_requested",
                            "stopped_by": "terminate_requested",
                        }
                    ],
                },
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["run_status"], "stop_requested")
            self.assertEqual(summary["liveness_status"], "stop_requested")
            self.assertEqual(summary["inflight"]["total"], 0)
            self.assertEqual(summary["inactive_inflight"]["total"], 1)
            self.assertNotIn("agentteam watch", summary.get("next_action", ""))

    def test_agentteam_cli_status_prefers_active_worker_for_last_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "active-worker-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "running",
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
                            "agent_id": "implementation-worker-active",
                        }
                    ],
                    "steps": [],
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "running",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-active",
                            "worker_status": "running",
                            "worker_diagnostic_state": "processing",
                            "last_activity": "processing",
                            "heartbeat_task_id": "optimize-pipeline",
                            "heartbeat_progress_summary": "reading target files",
                        },
                        {
                            "worker_agent_id": "implementation-worker-old",
                            "worker_status": "stopped",
                            "exit_code": 0,
                        },
                    ],
                },
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertIn("implementation-worker-active running", summary["last_worker"])
            self.assertIn("progress=reading target files", summary["last_worker"])

    def test_agentteam_cli_status_run_dir_does_not_require_project_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = _write_completed_operator_run(tmp_path / "work" / "runs" / "profileless-run")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=tmp_path,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["latest_run"], "profileless-run")
            self.assertEqual(summary["project"], "unknown")
            self.assertEqual(summary["run_dir"], str(run_dir.resolve()))

    def test_agentteam_cli_status_run_dir_uses_run_dir_work_root_over_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            profile_work_root = tmp_path / "profile-work"
            explicit_work_root = tmp_path / "explicit-work"
            run_dir = _write_completed_operator_run(explicit_work_root / "runs" / "explicit-run")
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, profile_work_root, "profile-project")
            _write_json(
                explicit_work_root / "pursue" / "explicit-run-goal-memory.json",
                {
                    "memory_schema_version": "goal_memory.v1",
                    "latest_taskpack_id": "explicit-run",
                },
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["project"], "profile-project")
            self.assertEqual(summary["latest_run"], "explicit-run")
            self.assertEqual(summary.get("projection_status"), "missing")
            self.assertEqual(
                summary.get("projection_db_path"),
                str((explicit_work_root / "agentteam.db").resolve()),
            )
            self.assertNotEqual(
                summary.get("projection_db_path"),
                str((profile_work_root / "agentteam.db").resolve()),
            )
            self.assertIn(
                "agentteam report --taskpack explicit-run",
                summary.get("next_action") or "",
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

    def test_status_includes_permission_request_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "permission-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "permission-project")
            _write_jsonl(
                run_dir / "events.jsonl",
                [
                    {
                        "event_id": "EVT-0001",
                        "event_type": "permission_request_required",
                        "sequence": 1,
                        "time": "2026-06-14T00:00:00Z",
                        "task_id": "optimize-pipeline",
                        "attempt_id": "ATTEMPT-001",
                        "lease_id": "LEASE-001",
                        "payload": {
                            "task_id": "optimize-pipeline",
                            "attempt_id": "ATTEMPT-001",
                            "lease_id": "LEASE-001",
                            "request_id": "PERM-001",
                            "request_type": "sandbox_permission",
                            "request_status": "waiting",
                            "requested_capability": "sandbox_escalation",
                            "reason": "network is restricted",
                            "scope": "next_attempt",
                            "sandbox": "workspace-write",
                            "command": ["codex", "exec"],
                        },
                    }
                ],
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("overall_status: permission_required", completed.stdout)
            self.assertIn("permission_requests: 1", completed.stdout)
            self.assertIn(
                "permission_request: PERM-001 task=optimize-pipeline capability=sandbox_escalation",
                completed.stdout,
            )
            self.assertIn("reason: network is restricted", completed.stdout)
            self.assertIn(
                f"approve: agentteam permissions approve --run-dir {run_dir.resolve()} --request-id PERM-001",
                completed.stdout,
            )
            self.assertIn(
                f"deny: agentteam permissions deny --run-dir {run_dir.resolve()} --request-id PERM-001",
                completed.stdout,
            )

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
            self.assertIn("projection_source: files", list_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", list_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", list_completed.stdout)
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

    def test_agentteam_cli_taskpack_list_can_read_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "list-db-project")
            _write_completed_operator_run(work_root / "runs" / "listed-db")
            _write_json(
                work_root / "frozen" / "listed-db" / "taskpack.json",
                {
                    "taskpack_id": "listed-db",
                    "goal": "List through projection DB.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
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

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            summary = json.loads(list_completed.stdout)
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["frozen_count"], 1)
            self.assertEqual(summary["taskpacks"][0]["taskpack_id"], "listed-db")
            self.assertEqual(summary["taskpacks"][0]["run_status"], "idle")

    def test_agentteam_cli_taskpack_list_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "list-db-project")
            _write_json(
                work_root / "frozen" / "listed-db" / "taskpack.json",
                {
                    "taskpack_id": "listed-db",
                    "goal": "List through projection DB.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)
            _write_json(
                work_root / "frozen" / "listed-after-rebuild" / "taskpack.json",
                {
                    "taskpack_id": "listed-after-rebuild",
                    "goal": "Force stale projection.",
                    "validation": {"status": "accepted"},
                },
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
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            summary = json.loads(list_completed.stdout)
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertEqual(summary["frozen_count"], 2)
            self.assertEqual(
                {item["taskpack_id"] for item in summary["taskpacks"]},
                {"listed-db", "listed-after-rebuild"},
            )

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
            self.assertEqual(
                Path(release["launcher_path"]),
                release_root / "agentteam",
            )
            self.assertEqual(
                Path(release["runtime_root"]),
                release_root
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime",
            )
            self.assertEqual(summary["active_release"]["release_id"], release_id)
            self.assertEqual(Path(summary["active_release"]["release_root"]), release_root)
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue(Path(release["runtime_root"]).is_dir())
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
            schemas = checkout / "experiments" / "native_agentteam_runtime" / "schemas"
            runtime_pkg.mkdir(parents=True)
            schemas.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# fixture runtime\n", encoding="utf-8")
            _write_json(schemas / "taskpack_blueprint.schema.json", {"type": "object"})
            _write_json(schemas / "p0_experiment_readiness.schema.json", {"type": "object"})
            _write_json(schemas / "experiment_manifest.schema.json", {"type": "object"})
            _write_json(
                runtime_pkg / "data" / "p0_experiment_readiness.v1.json",
                {"schema_version": "p0_experiment_readiness.v1"},
            )
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
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "taskpack_blueprint.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "p0_experiment_readiness.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "schemas"
                    / "experiment_manifest.schema.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    release_root
                    / "experiments"
                    / "native_agentteam_runtime"
                    / "m0_runtime"
                    / "agentteam_runtime"
                    / "data"
                    / "p0_experiment_readiness.v1.json"
                ).is_file()
            )
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
            for release_id in [
                "active-release",
                "frozen-release",
                "running-release",
                "idle-release",
            ]:
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
            frozen_taskpack = (
                work_root
                / "frozen"
                / "v2"
                / "approved-before-run"
            )
            frozen_taskpack.mkdir(parents=True)
            _write_json(
                frozen_taskpack / "taskpack.yaml",
                {
                    "taskpack_id": "approved-before-run",
                    "status": "frozen",
                    "context": {
                        "runtime_release_id": "frozen-release",
                    },
                },
            )

            result = prune_releases(work_root, keep_latest=1)

            self.assertEqual(result["deleted_release_ids"], ["idle-release"])
            self.assertEqual(
                result["protected_release_ids"],
                [
                    "active-release",
                    "frozen-release",
                    "running-release",
                ],
            )
            self.assertTrue((releases / "active-release").exists())
            self.assertTrue((releases / "frozen-release").exists())
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

    def test_global_release_prune_explains_protected_and_orphaned_roots(self):
        from agentteam_runtime.release_manager import prune_global_releases

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            global_store = tmp_path / "agentteam-home" / "runtime-releases"
            source_root = global_store / "source-a"
            work_root = tmp_path / "agentteam-home" / "project-a"
            other_work_root = tmp_path / "agentteam-home" / "project-b"

            release_roots = {}
            for release_id in [
                "active-release",
                "frozen-release",
                "ref-release",
                "running-release",
                "orphan-release",
            ]:
                release_root = source_root / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_schema_version": "agentteam_release_manifest.v2",
                            "install_method": "git_ref",
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_key": "source-a",
                            "installed_at": f"2026-06-12T00:00:0{len(release_roots)}Z",
                        }
                    ),
                    encoding="utf-8",
                )
                release_roots[release_id] = release_root

            (work_root / "releases").mkdir(parents=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(release_roots["active-release"]),
                    }
                ),
                encoding="utf-8",
            )
            refs_root = work_root / "releases" / "refs"
            refs_root.mkdir(parents=True)
            (refs_root / "ref-release.json").write_text(
                json.dumps(
                    {
                        "release_id": "ref-release",
                        "release_root": str(release_roots["ref-release"]),
                    }
                ),
                encoding="utf-8",
            )
            (refs_root / "frozen-release.json").write_text(
                json.dumps(
                    {
                        "release_id": "frozen-release",
                        "release_root": str(release_roots["frozen-release"]),
                    }
                ),
                encoding="utf-8",
            )
            frozen_taskpack = work_root / "frozen" / "v3" / "approved-before-run"
            frozen_taskpack.mkdir(parents=True)
            _write_json(
                frozen_taskpack / "taskpack.yaml",
                {
                    "taskpack_id": "approved-before-run",
                    "status": "frozen",
                    "context": {
                        "runtime_release_id": "frozen-release",
                    },
                },
            )
            running_state = other_work_root / "runs" / "running-run" / "state"
            running_state.mkdir(parents=True)
            (running_state / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "runtime_release_id": "running-release",
                        "runtime_release_root": str(release_roots["running-release"]),
                    }
                ),
                encoding="utf-8",
            )

            dry_run = prune_global_releases(
                work_root,
                release_store_root=global_store,
                force=False,
            )

            statuses = {item["release_id"]: item for item in dry_run["global_releases"]}
            self.assertEqual(dry_run["prune_status"], "dry_run")
            self.assertEqual(dry_run["deletable_global_release_ids"], ["orphan-release"])
            self.assertEqual(
                dry_run["protected_global_release_ids"],
                [
                    "active-release",
                    "frozen-release",
                    "ref-release",
                    "running-release",
                ],
            )
            self.assertIn("active_project", statuses["active-release"]["protection_reasons"])
            self.assertIn(
                "frozen_taskpack",
                statuses["frozen-release"]["protection_reasons"],
            )
            self.assertIn("project_ref", statuses["ref-release"]["protection_reasons"])
            self.assertIn("nonterminal_run", statuses["running-release"]["protection_reasons"])
            self.assertEqual(statuses["orphan-release"]["status"], "deletable")
            self.assertTrue(release_roots["orphan-release"].exists())

            pruned = prune_global_releases(
                work_root,
                release_store_root=global_store,
                force=True,
            )

            self.assertEqual(pruned["prune_status"], "pruned")
            self.assertEqual(pruned["deleted_global_release_ids"], ["orphan-release"])
            self.assertTrue(release_roots["active-release"].exists())
            self.assertTrue(release_roots["frozen-release"].exists())
            self.assertTrue(release_roots["ref-release"].exists())
            self.assertTrue(release_roots["running-release"].exists())
            self.assertFalse(release_roots["orphan-release"].exists())

    def test_agentteam_cli_gc_global_releases_requires_force_and_deletes_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            agentteam_home = tmp_path / "agentteam-home"
            global_store = agentteam_home / "runtime-releases"
            work_root = agentteam_home / "gc-global-project"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-global-project")

            source_root = global_store / "source-a"
            active_release = source_root / "active-release"
            orphan_release = source_root / "orphan-release"
            for release_id, release_root in [
                ("active-release", active_release),
                ("orphan-release", orphan_release),
            ]:
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "manifest_schema_version": "agentteam_release_manifest.v2",
                            "install_method": "git_ref",
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_key": "source-a",
                            "installed_at": "2026-06-12T00:00:00Z",
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases").mkdir(parents=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "active-release",
                        "release_root": str(active_release),
                    }
                ),
                encoding="utf-8",
            )

            env = _test_env()
            env["AGENTTEAM_RUNTIME_RELEASE_ROOT"] = str(global_store)
            dry_run_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--global-releases",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(dry_run_completed.returncode, 0, dry_run_completed.stderr)
            dry_run_summary = json.loads(dry_run_completed.stdout)
            self.assertEqual(dry_run_summary["gc_status"], "dry_run")
            self.assertEqual(
                dry_run_summary["global_release_prune"]["deletable_global_release_ids"],
                ["orphan-release"],
            )
            self.assertTrue(orphan_release.exists())

            force_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--global-releases",
                    "--force",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(force_completed.returncode, 0, force_completed.stderr)
            force_summary = json.loads(force_completed.stdout)
            self.assertEqual(force_summary["gc_status"], "completed")
            self.assertEqual(
                force_summary["global_release_prune"]["deleted_global_release_ids"],
                ["orphan-release"],
            )
            self.assertTrue(active_release.exists())
            self.assertFalse(orphan_release.exists())

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
        self.assertIn("pursue", help_completed.stdout)
        self.assertIn("agentteam help <command>", help_completed.stdout)
        self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
        self.assertIn("agentteam stop", stop_completed.stdout)
        self.assertIn("Stop or clean up an existing run", stop_completed.stdout)
        self.assertIn("agentteam stop --project-root <repo>", stop_completed.stdout)
        self.assertIn("--stale", stop_completed.stdout)

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

    def test_agentteam_cli_pursue_runs_one_fake_round_and_stops_on_review_gate(self):
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
                    "1",
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
            self.assertEqual(summary["pursue_status"], "stopped")
            self.assertEqual(summary["stop_reason"], "review_gate_required")
            self.assertEqual(summary["rounds_completed"], 1)
            self.assertEqual(summary["runs"][0]["round"], 1)
            self.assertEqual(summary["runs"][0]["taskpack_id"], "pursue-loop")
            self.assertEqual(summary["runs"][0]["status"], "completed")
            self.assertEqual(
                summary["runs"][0]["follow_up_recommendation"]["action"],
                "integrate",
            )
            self.assertTrue((work_root / "runs" / "pursue-loop").exists())
            recap_path = Path(summary["pursue_recap_path"])
            self.assertTrue(recap_path.exists())
            self.assertIn(str(work_root.resolve()), str(recap_path))
            recap = json.loads(recap_path.read_text(encoding="utf-8"))
            self.assertEqual(recap["pursue_id"], "pursue-loop")
            self.assertEqual(recap["rounds_completed"], 1)
            self.assertEqual(recap["max_rounds"], 1)
            self.assertEqual(recap["stop_reason"], "review_gate_required")
            self.assertEqual(recap["latest_taskpack_id"], "pursue-loop")
            self.assertEqual(
                recap["latest_report_path"],
                summary["runs"][0]["report_path"],
            )
            self.assertIn(
                "agentteam report --taskpack pursue-loop",
                recap["operator_next_action"],
            )

    def test_agentteam_cli_status_and_report_surface_latest_pursue_recap(self):
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
            pursue_completed = subprocess.run(
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
                    "1",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(pursue_completed.returncode, 0, pursue_completed.stderr)

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
            self.assertIn("pursue: pursue-loop stopped because review_gate_required", status_completed.stdout)
            self.assertIn("pursue_rounds: 1/1", status_completed.stdout)
            self.assertIn("pursue_latest_taskpack: pursue-loop", status_completed.stdout)
            self.assertIn("pursue_next_action: agentteam report --taskpack pursue-loop", status_completed.stdout)

            report_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "pursue-loop",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(report_completed.returncode, 0, report_completed.stderr)
            self.assertIn("## Pursue Recap", report_completed.stdout)
            self.assertIn("- Stop reason: review_gate_required", report_completed.stdout)
            self.assertIn("- Latest taskpack: pursue-loop", report_completed.stdout)
            self.assertIn("- Next action: agentteam report --taskpack pursue-loop", report_completed.stdout)

    def test_pursue_status_text_surfaces_all_operator_stop_reasons(self):
        base_summary = {
            "project": "pursue-project",
            "latest_run": "pursue-loop",
            "status": "completed",
            "overall_status": "completed",
            "run_status": "completed",
            "liveness_status": "stopped",
            "tasks": {"done": 1, "blocked": 0},
            "integration": {"blocked": 0},
            "evidence": {},
            "integration_baseline": {},
            "token_usage": {},
            "inflight": {"total": 0},
            "manual_gates": 0,
            "permission_requests": 0,
            "workers": {"total": 0, "stopped": 0, "running": 0, "quarantined": 0},
            "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
        }
        for stop_reason in [
            "review_gate_required",
            "blocked",
            "manual_gate_required",
            "permission_request_required",
            "failed",
            "follow_up_queue_empty",
            "max_rounds_reached",
        ]:
            summary = {
                **base_summary,
                "pursue_recap": {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 1,
                    "max_rounds": 2,
                    "stop_reason": stop_reason,
                    "latest_taskpack_id": "pursue-loop",
                    "latest_report_path": "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md",
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                },
            }
            with self.subTest(stop_reason=stop_reason):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _write_status_text(summary)
                output = buffer.getvalue()
                self.assertIn(f"pursue: pursue-loop stopped because {stop_reason}", output)

    def test_status_guidance_skips_pursue_queue_action_when_queue_has_no_auto_goal(self):
        summary = {
            "project": "pursue-project",
            "latest_run": "pursue-loop-r2",
            "status": "completed",
            "overall_status": "completed",
            "run_status": "completed",
            "liveness_status": "idle",
            "tasks": {"done": 1, "blocked": 0},
            "integration": {"blocked": 0},
            "integration_baseline": {
                "branch": "agentteam/run/pursue-loop-r2/integration",
                "head": "abc123",
                "worktree_exists": True,
                "worktree": "/tmp/agentteam-work/runs/pursue-loop-r2/integration/pursue-loop-r2",
            },
            "manual_gates": 0,
            "permission_requests": 0,
            "pursue_recap": {
                "pursue_id": "pursue-loop",
                "latest_taskpack_id": "pursue-loop-r2",
                "operator_next_action": "agentteam queue next --taskpack pursue-loop-r2",
                "latest_follow_up_queue": {
                    "queue_status": "no_auto_dispatchable_items",
                    "item_count": 3,
                },
            },
            "run_dir": "/tmp/agentteam-work/runs/pursue-loop-r2",
        }

        guidance = agentteam_module._status_operator_guidance(summary)

        self.assertNotIn("agentteam queue next", guidance["next_action"])
        self.assertIn("agentteam report --taskpack pursue-loop-r2", guidance["next_action"])
        self.assertIn("integration baseline", guidance["operator_hint"])

    def test_status_guidance_recomputes_stale_pursue_queue_before_recommending_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop-r2"
            report_path = run_dir / "reports" / "final_report.md"
            operator_report = {
                "report_schema_version": "operator_run_report.v1",
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [
                    {
                        "task_id": "queue-quality",
                        "attempt_id": "queue-quality-ATTEMPT-001",
                        "status": "implementation completed",
                        "what_changed": ["Added a narrow queue test."],
                        "changed_files": ["tests/test_taskpack.py"],
                        "verification": ["focused queue test passed"],
                        "integration": "passed",
                        "merge_recommendation": "Review before merging.",
                        "next_steps": [
                            "由 operator 审阅并决定是否集成本测试变更。",
                            (
                                "后续可继续用 `pursue_recap.structured_evidence` "
                                "驱动 `agentteam queue next` 或 report 摘要的跨轮检查。"
                            ),
                        ],
                    }
                ],
            }
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/pursue-loop-r2/integration",
                        "integration_baseline_worktree_path": str(
                            (run_dir / "integration-baseline").resolve()
                        ),
                        "integration_baseline_head_sha": "abc123",
                    },
                    "backlog": {"items": [{"task_id": "queue-quality", "backlog_status": "done"}]},
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
            goal_memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _write_json(
                goal_memory_path,
                {
                    "pursue_id": "pursue-loop",
                    "work_root": str(work_root),
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_run_ids": ["pursue-loop-r2"],
                    "follow_up_queue": [
                        {
                            "objective": (
                                "用新的 `pursue_recap.structured_evidence` "
                                "驱动后续 `agentteam queue next` 或 report 摘要的跨轮检查。"
                            ),
                            "source_taskpack_id": "pursue-loop",
                            "source_report_path": str(report_path),
                        }
                    ],
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "pursue_status": "stopped",
                    "rounds_completed": 2,
                    "max_rounds": 2,
                    "stop_reason": "max_rounds_reached",
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_report_path": str(report_path),
                    "goal_memory_path": str(goal_memory_path),
                    "operator_next_action": "agentteam queue next --taskpack pursue-loop-r2",
                    "latest_follow_up_queue": {
                        "queue_status": "ready",
                        "next_goal": "由 operator 审阅并决定是否集成本测试变更。",
                    },
                    "runs": [{"taskpack_id": "pursue-loop-r2"}],
                },
            )

            summary = _build_run_status_summary(
                {"project_key": "pursue-project", "work_root": str(work_root)},
                run_dir,
            )

            self.assertNotIn("agentteam queue next", summary["next_action"])
            self.assertIn("agentteam report --taskpack pursue-loop-r2", summary["next_action"])
            self.assertIn("integration baseline", summary["operator_hint"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _write_status_text(summary)
            status_text = buffer.getvalue()
            self.assertIn(
                "pursue_next_action: agentteam queue next --taskpack pursue-loop-r2 (superseded by next_action)",
                status_text,
            )

    def test_pursue_stop_reason_blocks_operator_gates(self):
        self.assertEqual(
            _pursue_stop_reason({"status": "manual_gate_required"}),
            "manual_gate_required",
        )
        self.assertEqual(
            _pursue_stop_reason({"status": "permission_request_required"}),
            "permission_request_required",
        )
        self.assertEqual(
            _pursue_stop_reason({"status": "completed", "blocked_count": 1}),
            "blocked",
        )
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate"},
                }
            ),
            "review_gate_required",
        )
        self.assertIsNone(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate_then_next"},
                },
                allow_review_gate_follow_up=True,
            )
        )

    def test_pursue_stop_reason_blocks_stopped_runs_even_when_followup_is_allowed(self):
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "run_status": "stopped",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate_then_next"},
                },
                allow_review_gate_follow_up=True,
            ),
            "stopped",
        )
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "stopped",
                    "run_status": "running",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "next"},
                },
                allow_review_gate_follow_up=True,
            ),
            "stopped",
        )

    def test_pursue_loop_passes_previous_baseline_head_to_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            captured_submit_args = []
            source_reports = [
                {
                    "run_id": "pursue-loop",
                    "integration_baseline": {
                        "head_sha": "verified-head",
                        "branch": "agentteam/run/pursue-loop/integration",
                    },
                    "completion_summary": {
                        "next_steps": ["Continue with the next optimization task."]
                    },
                },
                {
                    "run_id": "pursue-loop-r2",
                    "integration_baseline": {
                        "head_sha": "round-two-head",
                        "branch": "agentteam/run/pursue-loop-r2/integration",
                    },
                    "completion_summary": {},
                },
            ]
            originals = {
                "_submit_args_from_profile": agentteam_module._submit_args_from_profile,
                "_handle_submit": agentteam_module._handle_submit,
                "build_run_completion_report": agentteam_module.build_run_completion_report,
                "build_goal_memory": agentteam_module.build_goal_memory,
                "write_goal_memory": agentteam_module.write_goal_memory,
                "_pursue_follow_up_queue_summary": agentteam_module._pursue_follow_up_queue_summary,
            }

            def fake_submit_args_from_profile(args, project_root, profile):
                return SimpleNamespace(
                    goal=args.goal,
                    taskpack_id=args.taskpack_id,
                    initial_integration_base_ref=None,
                )

            def fake_handle_submit(submit_args):
                captured_submit_args.append(
                    {
                        "taskpack_id": submit_args.taskpack_id,
                        "initial_integration_base_ref": getattr(
                            submit_args,
                            "initial_integration_base_ref",
                            None,
                        ),
                    }
                )
                action = "continue" if len(captured_submit_args) == 1 else "integrate"
                return {
                    "status": "completed",
                    "taskpack_id": submit_args.taskpack_id,
                    "report": {
                        "run_status": "completed",
                        "blocked_count": 0,
                        "completion_summary": {
                            "follow_up_recommendation": {"action": action}
                        },
                    },
                }

            def fake_build_run_completion_report(*args, **kwargs):
                return source_reports[len(captured_submit_args) - 1]

            def fake_build_goal_memory(**kwargs):
                return {}

            def fake_write_goal_memory(work_root_arg, goal_memory):
                memory_path = Path(work_root_arg) / "pursue" / "goal-memory.json"
                memory_path.parent.mkdir(parents=True, exist_ok=True)
                memory_path.write_text(json.dumps(goal_memory, sort_keys=True), encoding="utf-8")
                return memory_path

            def fake_queue_summary(**kwargs):
                return {
                    "queue_status": "ready",
                    "next_goal": "Continue with the next optimization task.",
                }

            try:
                agentteam_module._submit_args_from_profile = fake_submit_args_from_profile
                agentteam_module._handle_submit = fake_handle_submit
                agentteam_module.build_run_completion_report = fake_build_run_completion_report
                agentteam_module.build_goal_memory = fake_build_goal_memory
                agentteam_module.write_goal_memory = fake_write_goal_memory
                agentteam_module._pursue_follow_up_queue_summary = fake_queue_summary

                result = agentteam_module._run_pursue_loop(
                    SimpleNamespace(
                        work_root=str(work_root),
                        taskpack_id="pursue-loop",
                        goal="Improve the project.",
                        json=True,
                        allow_review_gate_follow_up=False,
                    ),
                    project_root=Path(tmp) / "repo",
                    profile={"work_root": str(work_root), "project_key": "pursue-project"},
                    goal="Improve the project.",
                    max_rounds=2,
                )
            finally:
                for name, value in originals.items():
                    setattr(agentteam_module, name, value)

            self.assertEqual(len(captured_submit_args), 2)
            self.assertIsNone(captured_submit_args[0]["initial_integration_base_ref"])
            self.assertEqual(
                captured_submit_args[1]["initial_integration_base_ref"],
                "verified-head",
            )
            self.assertEqual(
                result["runs"][1]["initial_integration_base_ref"],
                "verified-head",
            )

    def test_pursue_loop_reuses_previous_repo_map_handoff_on_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            baseline = tmp_path / "integration-baseline"
            _write_json(
                baseline / taskpack_module.REPO_MAP_HANDOFF_PATH,
                {"schema_version": "repo_map_handoff.v1"},
            )
            captured_submit_args = []
            source_reports = [
                {
                    "run_id": "pursue-loop",
                    "integration_baseline": {
                        "head_sha": "verified-head",
                        "worktree_path": str(baseline),
                        "worktree_exists": True,
                    },
                    "completion_summary": {
                        "next_steps": ["Continue with the next optimization task."]
                    },
                },
                {
                    "run_id": "pursue-loop-r2",
                    "integration_baseline": {"head_sha": "round-two-head"},
                    "completion_summary": {},
                },
            ]
            originals = {
                "_submit_args_from_profile": agentteam_module._submit_args_from_profile,
                "_handle_submit": agentteam_module._handle_submit,
                "build_run_completion_report": agentteam_module.build_run_completion_report,
                "build_goal_memory": agentteam_module.build_goal_memory,
                "write_goal_memory": agentteam_module.write_goal_memory,
                "_pursue_follow_up_queue_summary": agentteam_module._pursue_follow_up_queue_summary,
            }

            def fake_submit_args_from_profile(args, project_root, profile):
                return SimpleNamespace(
                    goal=args.goal,
                    taskpack_id=args.taskpack_id,
                    initial_integration_base_ref=None,
                    reuse_repo_map_handoff_path=None,
                )

            def fake_handle_submit(submit_args):
                captured_submit_args.append(
                    {
                        "taskpack_id": submit_args.taskpack_id,
                        "reuse_repo_map_handoff_path": getattr(
                            submit_args,
                            "reuse_repo_map_handoff_path",
                            None,
                        ),
                    }
                )
                action = "continue" if len(captured_submit_args) == 1 else "integrate"
                return {
                    "status": "completed",
                    "taskpack_id": submit_args.taskpack_id,
                    "report": {
                        "run_status": "completed",
                        "blocked_count": 0,
                        "completion_summary": {
                            "follow_up_recommendation": {"action": action}
                        },
                    },
                }

            def fake_build_run_completion_report(*args, **kwargs):
                return source_reports[len(captured_submit_args) - 1]

            def fake_build_goal_memory(**kwargs):
                return {}

            def fake_write_goal_memory(work_root_arg, goal_memory):
                memory_path = Path(work_root_arg) / "pursue" / "goal-memory.json"
                memory_path.parent.mkdir(parents=True, exist_ok=True)
                memory_path.write_text(json.dumps(goal_memory, sort_keys=True), encoding="utf-8")
                return memory_path

            def fake_queue_summary(**kwargs):
                return {
                    "queue_status": "ready",
                    "next_goal": "Continue with the next optimization task.",
                }

            try:
                agentteam_module._submit_args_from_profile = fake_submit_args_from_profile
                agentteam_module._handle_submit = fake_handle_submit
                agentteam_module.build_run_completion_report = fake_build_run_completion_report
                agentteam_module.build_goal_memory = fake_build_goal_memory
                agentteam_module.write_goal_memory = fake_write_goal_memory
                agentteam_module._pursue_follow_up_queue_summary = fake_queue_summary

                result = agentteam_module._run_pursue_loop(
                    SimpleNamespace(
                        work_root=str(work_root),
                        taskpack_id="pursue-loop",
                        goal="Improve the project.",
                        json=True,
                        allow_review_gate_follow_up=False,
                    ),
                    project_root=tmp_path / "repo",
                    profile={"work_root": str(work_root), "project_key": "pursue-project"},
                    goal="Improve the project.",
                    max_rounds=2,
                )
            finally:
                for name, value in originals.items():
                    setattr(agentteam_module, name, value)

            self.assertIsNone(captured_submit_args[0]["reuse_repo_map_handoff_path"])
            self.assertEqual(
                captured_submit_args[1]["reuse_repo_map_handoff_path"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )
            self.assertEqual(
                result["runs"][1]["repo_map_handoff_reuse"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )

    def test_pursue_next_goal_uses_report_next_step(self):
        self.assertEqual(
            _pursue_next_goal(
                "持续优化原始目标。",
                {"completion_summary": {"next_steps": ["继续验证最慢模块。"]}},
            ),
            "继续验证最慢模块。",
        )
        self.assertIn(
            "持续优化原始目标。",
            _pursue_next_goal("持续优化原始目标。", {"completion_summary": {}}),
        )

    def test_pursue_follow_up_queue_summary_selects_next_goal(self):
        from agentteam_runtime.agentteam import _pursue_follow_up_queue_summary

        summary = _pursue_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续验证最慢模块。"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": 'agentteam next --from-taskpack first-pass --goal "补充准确率基准。"',
                    },
                },
            },
            goal_memory={
                "memory_path": "/tmp/work/pursue/first-pass-goal-memory.json",
                "follow_up_queue": [
                    {
                        "objective": "检查端到端延迟。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    }
                ],
            },
            source_run_dir=Path("/tmp/work/runs/first-pass"),
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(summary["source_taskpack_id"], "first-pass")
        self.assertEqual(summary["next_goal"], "继续验证最慢模块。")
        self.assertIn("agentteam next --from-taskpack first-pass", summary["next_command"])
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["继续验证最慢模块。", "补充准确率基准。", "检查端到端延迟。"],
        )

    def test_goal_memory_bounds_round_history_and_prompt_text(self):
        long_text = "继续验证候选实现并记录基准。" * 40
        rounds = [
            {
                "round": index,
                "taskpack_id": f"pursue-loop-r{index}",
                "status": "completed",
                "run_status": "completed",
                "blocked_count": index % 2,
                "report_path": f"/tmp/reports/r{index}.md",
            }
            for index in range(1, 8)
        ]

        memory = build_goal_memory(
            pursue_id="pursue-loop",
            original_goal=long_text,
            work_root="/tmp/agentteam-work",
            rounds=rounds,
            source_report={
                "report_path": "/tmp/reports/r7.md",
                "completion_summary": {
                    "what_changed": [long_text],
                    "next_steps": [long_text],
                    "evidence_gaps": [long_text],
                },
            },
            stop_reason="blocked",
            max_round_history=3,
            max_text_chars=80,
            max_queue_items=2,
        )
        rendered = render_goal_memory_prompt_context(memory)

        self.assertEqual(memory["memory_schema_version"], "goal_memory.v1")
        self.assertEqual([item["round"] for item in memory["round_history"]], [5, 6, 7])
        self.assertEqual(memory["latest_run_ids"], ["pursue-loop-r5", "pursue-loop-r6", "pursue-loop-r7"])
        self.assertLessEqual(len(memory["original_goal"]), 80)
        self.assertLessEqual(len(memory["current_hypothesis"]), 80)
        self.assertLessEqual(len(memory["current_next_step"]), 80)
        self.assertLessEqual(len(memory["blocked_reasons"][0]), 80)
        self.assertLessEqual(len(memory["follow_up_queue"]), 2)
        self.assertIn("Long-goal memory:", rendered)
        self.assertIn("memory_bounds: round_history<=3 text<=80 queue<=2", rendered)

    def test_goal_memory_carries_latest_round_recap_for_followup_context(self):
        report_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md"
        report_json_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.json"

        memory = build_goal_memory(
            pursue_id="pursue-loop",
            original_goal="持续增强 AgentTeam 长期任务可靠性。",
            work_root="/tmp/agentteam-work",
            rounds=[
                {
                    "round": 1,
                    "taskpack_id": "pursue-loop",
                    "status": "completed",
                    "run_status": "completed",
                    "blocked_count": 1,
                    "report_path": report_path,
                }
            ],
            source_report={
                "run_id": "pursue-loop",
                "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
                "run_status": "completed",
                "run_outcome": "completed_with_review_required",
                "blocked_count": 1,
                "report_path": report_path,
                "report_json_path": report_json_path,
                "token_usage": {
                    "usage_status": "reported",
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "total_tokens": 1500,
                },
                "completion_summary": {
                    "what_changed": ["持久化上一轮 recap。"],
                    "changed_files": ["agentteam_runtime/goal_memory.py"],
                    "verification": ["python3 -m unittest test_taskpack.GoalMemory passed"],
                    "measured_results": ["latest recap persisted"],
                    "next_steps": ["继续实现 queue recap 展示。"],
                    "evidence_gaps": ["需要 operator review。"],
                },
            },
            stop_reason="review_gate_required",
        )
        rendered = render_goal_memory_prompt_context(memory)

        latest = memory["latest_round_recap"]
        self.assertEqual(latest["taskpack_id"], "pursue-loop")
        self.assertEqual(latest["result_status"], "completed")
        self.assertEqual(latest["run_outcome"], "completed_with_review_required")
        self.assertEqual(latest["stop_reason"], "review_gate_required")
        self.assertEqual(latest["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(latest["token_usage"]["usage_status"], "reported")
        self.assertEqual(latest["token_usage"]["total_tokens"], 1500)
        self.assertIn({"type": "report", "path": report_path}, latest["evidence_paths"])
        self.assertIn({"type": "report_json", "path": report_json_path}, latest["evidence_paths"])
        self.assertIn("需要 operator review。", latest["blockers"])
        self.assertEqual(memory["round_history"][-1]["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(
            memory["follow_up_queue"][0]["source_evidence_paths"],
            latest["evidence_paths"],
        )
        self.assertEqual(memory["follow_up_queue"][0]["stop_reason"], "review_gate_required")
        self.assertEqual(memory["follow_up_queue"][0]["token_usage"]["total_tokens"], 1500)
        self.assertIn("latest_result: completed_with_review_required", rendered)
        self.assertIn(f"latest_evidence_path: {report_path}", rendered)
        self.assertIn("latest_stop_reason: review_gate_required", rendered)
        self.assertIn("latest_token_usage: Token usage: total=1500 input=1200 output=300 reported=1/1", rendered)
        self.assertIn("latest_recommended_next_step: 继续实现 queue recap 展示。", rendered)

    def test_follow_up_queue_summary_merges_report_and_goal_memory(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续验证最慢模块。", "补充准确率基准。"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": 'agentteam next --from-taskpack first-pass --goal "继续验证最慢模块。"',
                    },
                },
            },
            goal_memory={
                "follow_up_queue": [
                    {
                        "objective": "补充准确率基准。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    },
                    {
                        "objective": "检查端到端延迟。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    },
                ]
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(summary["source_taskpack_id"], "first-pass")
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["继续验证最慢模块。", "补充准确率基准。", "检查端到端延迟。"],
        )
        self.assertEqual(summary["next_goal"], "继续验证最慢模块。")
        self.assertEqual(
            summary["next_command"],
            'agentteam next --from-taskpack first-pass --goal "继续验证最慢模块。"',
        )

    def test_follow_up_queue_summary_skips_generic_next_step_for_concrete_recommendation(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续优化"],
                    "changed_files": ["agentteam_runtime/follow_up_queue.py"],
                    "verification": ["python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed"],
                    "measured_results": ["generic next_steps no longer selected verbatim"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": (
                            'agentteam next --from-taskpack first-pass --goal '
                            '"补充 follow_up_queue next_goal 具体化回归测试。"'
                        ),
                    },
                },
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["补充 follow_up_queue next_goal 具体化回归测试。"],
        )
        self.assertEqual(summary["next_goal"], "补充 follow_up_queue next_goal 具体化回归测试。")
        self.assertEqual(
            summary["next_command"],
            'agentteam next --from-taskpack first-pass --goal "补充 follow_up_queue next_goal 具体化回归测试。"',
        )

    def test_follow_up_queue_prefers_actionable_step_over_review_only_step(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "agentteam-long-run-reliability-003-r2",
                "report_path": (
                    "/tmp/agentteam-long-run-reliability/runs/"
                    "agentteam-long-run-reliability-003-r2/reports/final_report.md"
                ),
                "completion_summary": {
                    "next_steps": [
                        (
                            "operator 审阅 `operator_report.py` 与 `test_taskpack.py` 的 diff，"
                            "确认 scoped `review_gate` 语义符合 AgentTeam-as-target 策略。"
                        ),
                    ],
                    "follow_up_recommendation": {
                        "action": "integrate_then_next",
                        "next_command": (
                            "agentteam next --from-taskpack agentteam-long-run-reliability-003-r2 "
                            '--goal "operator 审阅 `operator_report.py` 与 `test_taskpack.py` 的 diff，'
                            '确认 scoped `review_gate` 语义符合 AgentTeam-as-target 策略。"'
                        ),
                    },
                },
            },
            goal_memory={
                "memory_path": (
                    "/tmp/agentteam-long-run-reliability/pursue/"
                    "agentteam-long-run-reliability-003-goal-memory.json"
                ),
                "follow_up_queue": [
                    {
                        "objective": "补充 queue selector 的 operator-integrated 后续验证测试。",
                        "source_taskpack_id": "agentteam-long-run-reliability-004",
                        "source_report_path": "/tmp/agentteam-long-run-reliability/runs/004/report.md",
                        "readiness": "ready",
                    }
                ],
            },
            source_taskpack_id="agentteam-long-run-reliability-003-r2",
            limit=5,
        )

        self.assertEqual(
            summary["next_goal"],
            "补充 queue selector 的 operator-integrated 后续验证测试。",
        )
        self.assertEqual(summary["selected_item"]["source"], "goal_memory.follow_up_queue")
        self.assertTrue(
            any(
                item["objective"].startswith("operator 审阅")
                for item in summary["items"]
                if isinstance(item, dict)
            )
        )

    def test_follow_up_queue_has_no_next_goal_for_review_and_process_only_items(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "db-longrun-observability-r5-r2",
                "report_path": (
                    "/tmp/agentteam-long-run-reliability/runs/"
                    "db-longrun-observability-r5-r2/reports/final_report.md"
                ),
                "completion_summary": {
                    "next_steps": [
                        "由 operator 审阅并决定是否集成本测试变更。",
                        (
                            "后续可继续用 `pursue_recap.structured_evidence` "
                            "驱动 `agentteam queue next` 或 report 摘要的跨轮检查。"
                        ),
                        "继续保持 semantic authority、source merge、push、release activation 由 operator gate 控制。",
                    ],
                    "follow_up_recommendation": {
                        "action": "integrate_then_next",
                        "next_command": (
                            "agentteam next --from-taskpack db-longrun-observability-r5-r2 "
                            '--goal "由 operator 审阅并决定是否集成本测试变更。"'
                        ),
                    },
                },
            },
            source_taskpack_id="db-longrun-observability-r5-r2",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "no_auto_dispatchable_items")
        self.assertIsNone(summary["selected_item"])
        self.assertIsNone(summary["next_goal"])
        self.assertIsNone(summary["next_command"])
        self.assertEqual(summary["items"][0]["objective"], "由 operator 审阅并决定是否集成本测试变更。")
        self.assertIn("operator_hint", summary)

    def test_compact_pursue_queue_summary_preserves_missing_selected_item(self):
        compact = agentteam_module._compact_pursue_queue_summary(
            {
                "queue_status": "no_auto_dispatchable_items",
                "source_taskpack_id": "pursue-loop-r2",
                "item_count": 1,
                "next_goal": None,
                "next_command": None,
                "selected_item": None,
                "items": [
                    {
                        "objective": "由 operator 审阅并决定是否集成本测试变更。",
                        "source": "report.next_steps",
                    }
                ],
            }
        )

        self.assertEqual(compact["queue_status"], "no_auto_dispatchable_items")
        self.assertIsNone(compact["next_goal"])
        self.assertNotIn("selected_item", compact)

    def test_follow_up_queue_summary_enriches_generic_next_step_with_report_evidence(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续优化"],
                    "changed_files": ["agentteam_runtime/follow_up_queue.py"],
                    "verification": ["python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed"],
                    "measured_results": ["generic next_steps no longer selected verbatim"],
                },
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertNotEqual(summary["next_goal"], "继续优化")
        self.assertIn("继续优化", summary["next_goal"])
        self.assertIn("基于上一轮证据", summary["next_goal"])
        self.assertIn("changed_files=agentteam_runtime/follow_up_queue.py", summary["next_goal"])
        self.assertIn(
            "verification=python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed",
            summary["next_goal"],
        )
        self.assertIn(
            "measured_result=generic next_steps no longer selected verbatim",
            summary["next_goal"],
        )
        self.assertIn(summary["next_goal"], summary["next_command"])

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

    def test_follow_up_queue_text_surfaces_goal_memory_recap_fields(self):
        from agentteam_runtime.follow_up_queue import (
            build_follow_up_queue_summary,
            render_follow_up_queue_text,
        )

        report_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md"
        summary = build_follow_up_queue_summary(
            source_report={"run_id": "pursue-loop-r2"},
            goal_memory={
                "memory_path": "/tmp/agentteam-work/pursue/pursue-loop-goal-memory.json",
                "follow_up_queue": [
                    {
                        "objective": "继续实现 queue recap 展示。",
                        "source_taskpack_id": "pursue-loop",
                        "source_report_path": report_path,
                        "source_result_status": "completed",
                        "source_run_outcome": "completed_with_review_required",
                        "source_evidence_paths": [{"type": "report", "path": report_path}],
                        "stop_reason": "review_gate_required",
                        "token_usage": {
                            "usage_status": "unavailable",
                            "reported_attempt_count": 0,
                            "unreported_attempt_count": 1,
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                        },
                        "blockers": ["需要 operator review。"],
                        "suggested_verification": "python3 -m unittest test_taskpack.FollowUpQueue passed",
                    }
                ],
            },
            source_taskpack_id="pursue-loop-r2",
            limit=5,
        )

        selected = summary["selected_item"]
        self.assertEqual(selected["source"], "goal_memory.follow_up_queue")
        self.assertEqual(selected["source_result_status"], "completed")
        self.assertEqual(selected["source_run_outcome"], "completed_with_review_required")
        self.assertEqual(selected["source_evidence_paths"], [{"type": "report", "path": report_path}])
        self.assertEqual(selected["stop_reason"], "review_gate_required")
        self.assertEqual(selected["token_usage"]["usage_status"], "unavailable")
        self.assertEqual(selected["blockers"], ["需要 operator review。"])

        text = render_follow_up_queue_text(summary, next_only=True)

        self.assertIn("selected_result: completed_with_review_required", text)
        self.assertIn(f"selected_evidence_path: {report_path}", text)
        self.assertIn("selected_stop_reason: review_gate_required", text)
        self.assertIn("selected_token_usage: Token usage: unavailable", text)
        self.assertIn("selected_blockers: 需要 operator review。", text)
        self.assertIn(
            "selected_verification: python3 -m unittest test_taskpack.FollowUpQueue passed",
            text,
        )

    def test_operator_report_loads_pursue_goal_memory_round_recap(self):
        from agentteam_runtime.operator_report import (
            find_pursue_recap_for_run,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop"
            run_dir.mkdir(parents=True)
            report_path = str(run_dir / "reports" / "final_report.md")
            memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_round_recap": {
                        "taskpack_id": "pursue-loop",
                        "result_status": "completed",
                        "run_outcome": "completed_with_review_required",
                        "stop_reason": "review_gate_required",
                        "recommended_next_step": "继续实现 queue recap 展示。",
                        "evidence_paths": [{"type": "report", "path": report_path}],
                        "blockers": ["需要 operator review。"],
                        "token_usage": {
                            "usage_status": "unavailable",
                            "reported_attempt_count": 0,
                            "unreported_attempt_count": 1,
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                        },
                    },
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 1,
                    "max_rounds": 2,
                    "stop_reason": "review_gate_required",
                    "latest_taskpack_id": "pursue-loop",
                    "latest_report_path": report_path,
                    "goal_memory_path": str(memory_path),
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                    "runs": [{"taskpack_id": "pursue-loop"}],
                },
            )

            recap = find_pursue_recap_for_run(run_dir)
            markdown = render_run_completion_report(
                {
                    "project": "agentteam",
                    "run_id": "pursue-loop",
                    "run_dir": str(run_dir),
                    "run_status": "completed",
                    "run_outcome": "completed_with_review_required",
                    "scheduler_status": "idle",
                    "task_count": 0,
                    "blocked_count": 0,
                    "token_usage": {},
                    "pursue_recap": recap,
                    "integration_baseline": {},
                    "completion_summary": {},
                    "operator_report": {},
                }
            )

        self.assertEqual(recap["latest_round_recap"]["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(recap["latest_round_recap"]["token_usage"]["usage_status"], "unavailable")
        self.assertIn("- Latest result: completed_with_review_required", markdown)
        self.assertIn(f"- Evidence: {report_path}", markdown)
        self.assertIn("- Token usage: unavailable", markdown)
        self.assertIn("- Recommended next step: 继续实现 queue recap 展示。", markdown)

    def test_operator_report_renders_chinese_digest_with_stop_reason_and_tokens(self):
        from agentteam_runtime.operator_report import render_run_completion_report

        markdown = render_run_completion_report(
            {
                "project": "agentteam",
                "run_id": "pursue-loop",
                "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
                "run_status": "completed",
                "run_outcome": "completed_with_review_required",
                "scheduler_status": "idle",
                "task_count": 1,
                "blocked_count": 0,
                "token_usage": {
                    "usage_status": "unavailable",
                    "reported_attempt_count": 0,
                    "unreported_attempt_count": 1,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                },
                "pursue_recap": {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 2,
                    "max_rounds": 4,
                    "stop_reason": "review_gate_required",
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                    "latest_round_recap": {
                        "recommended_next_step": "继续执行 bounded dogfood 验证。",
                    },
                },
                "integration_baseline": {},
                "completion_summary": {
                    "operator_digest": [
                        "为什么：为了让 operator 在多轮结束后看清本轮取舍。",
                        "做了什么：补充中文汇报字段映射。",
                        "涉及文件：agentteam_runtime/operator_report.py",
                        "验证结果：test_taskpack: passed",
                        "风险：真实 Feishu webhook 仍需 operator 环境验证。",
                        "下一步：运行 2 轮 AgentTeam-as-target dogfood。",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("## 中文工作汇报", markdown)
        self.assertIn("- 当前轮次：2/4", markdown)
        self.assertIn("- 停止原因：review_gate_required", markdown)
        self.assertIn("- 下一步：继续执行 bounded dogfood 验证。", markdown)
        self.assertIn("- 为什么：为了让 operator 在多轮结束后看清本轮取舍。", markdown)
        self.assertIn("- 涉及文件：agentteam_runtime/operator_report.py", markdown)
        self.assertIn("- 验证结果：test_taskpack: passed", markdown)
        self.assertIn("- 风险：真实 Feishu webhook 仍需 operator 环境验证。", markdown)
        self.assertIn("- Token usage: unavailable", markdown)

    def test_semantic_feedback_proposal_helper_writes_review_artifact(self):
        from agentteam_runtime.semantic_feedback import write_semantic_feedback_proposal

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            proposal = write_semantic_feedback_proposal(
                work_root=work_root,
                proposal_id="design-gap-1",
                source_report={
                    "run_id": "first-pass",
                    "run_dir": "/tmp/work/runs/first-pass",
                    "report_path": "/tmp/work/runs/first-pass/reports/final_report.md",
                },
                target_artifacts=["design/system.md"],
                summary="实现证据显示系统边界需要补充。",
                rationale="worker 在实现阶段发现设计文档没有说明 review gate 的归属。",
                created_by="test",
                created_at="2026-06-14T00:00:00Z",
            )

            proposal_path = Path(proposal["proposal_path"])
            self.assertTrue(proposal_path.exists())
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["proposal_schema_version"], "semantic_feedback_proposal.v1")
            self.assertEqual(payload["proposal_status"], "pending_review")
            self.assertEqual(payload["proposal_id"], "design-gap-1")
            self.assertEqual(payload["source_taskpack_id"], "first-pass")
            self.assertEqual(payload["source_report_path"], "/tmp/work/runs/first-pass/reports/final_report.md")
            self.assertEqual(payload["target_artifacts"], ["design/system.md"])
            self.assertEqual(payload["summary"], "实现证据显示系统边界需要补充。")
            self.assertEqual(payload["rationale"], "worker 在实现阶段发现设计文档没有说明 review gate 的归属。")
            self.assertIn("does not mutate", payload["authority_boundary"])

    def test_agentteam_cli_pursue_writes_goal_memory_and_reuses_it_in_followup_context(self):
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
            self.assertEqual(summary["rounds_completed"], 2)
            memory_path = Path(summary["goal_memory_path"])
            self.assertTrue(memory_path.exists())
            self.assertIn(str(work_root.resolve()), str(memory_path))
            self.assertEqual(memory_path.parent.name, "pursue")
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            self.assertEqual(memory["memory_schema_version"], "goal_memory.v1")
            self.assertEqual(memory["pursue_id"], "pursue-loop")
            self.assertEqual(memory["original_goal"], "持续优化这个仓库的准确率和延迟。")
            self.assertEqual(memory["rounds_completed"], 2)
            self.assertEqual(memory["latest_taskpack_id"], "pursue-loop-r2")
            self.assertEqual(memory["latest_run_ids"], ["pursue-loop", "pursue-loop-r2"])
            self.assertLessEqual(len(memory["round_history"]), memory["limits"]["max_round_history"])
            self.assertLessEqual(len(json.dumps(memory, ensure_ascii=False)), memory["limits"]["max_memory_json_chars"])
            self.assertIn("goal_memory_path", summary["runs"][0])
            self.assertEqual(summary["runs"][0]["follow_up_queue"]["queue_status"], "ready")
            self.assertEqual(
                summary["runs"][0]["selected_next_goal"],
                summary["runs"][0]["follow_up_queue"]["next_goal"],
            )
            self.assertIn("agentteam queue next --taskpack pursue-loop-r2", summary["operator_next_action"])

            followup_taskpack = json.loads(
                (work_root / "frozen" / "pursue-loop-r2" / "taskpack.yaml").read_text(encoding="utf-8")
            )
            followup_goal = followup_taskpack["goal"]
            self.assertIn("Long-goal memory:", followup_goal)
            self.assertIn("completed_rounds: 1", followup_goal)
            self.assertIn("latest_run_ids: pursue-loop", followup_goal)
            self.assertIn("memory_path:", followup_goal)
            self.assertIn(summary["runs"][0]["selected_next_goal"], followup_goal)

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

    def test_agentteam_cli_pursue_text_output_is_compact(self):
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
                    "1",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("pursue_status: stopped", completed.stdout)
            self.assertIn("rounds_completed: 1/1", completed.stdout)
            self.assertIn("stop_reason: review_gate_required", completed.stdout)
            self.assertIn("latest_taskpack_id: pursue-loop", completed.stdout)
            self.assertIn("latest_report:", completed.stdout)
            self.assertIn("pursue_recap:", completed.stdout)
            self.assertIn("operator_next_action: agentteam report --taskpack pursue-loop", completed.stdout)
            self.assertLessEqual(len([line for line in completed.stdout.splitlines() if line.strip()]), 7)

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

    def test_agentteam_cli_next_reuses_repo_map_handoff_from_previous_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            baseline = tmp_path / "integration-baseline"
            _init_repo(repo)
            profile = build_project_profile(
                repo,
                project_key="next-reuse-project",
                work_root=work_root,
                author_runtime="fake",
                default_runtime="fake",
                one_shot=True,
            )
            write_project_profile(repo, profile, force=True)
            _write_json(
                baseline / taskpack_module.REPO_MAP_HANDOFF_PATH,
                {"schema_version": "repo_map_handoff.v1"},
            )
            _write_json(
                work_root / "runs" / "first-pass" / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_branch": "agentteam/run/first-pass/integration",
                        "integration_baseline_worktree_path": str(baseline),
                        "integration_baseline_head_sha": "abc123",
                    },
                },
            )

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
                    "Implement the next optimization step using prior context.",
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
            self.assertEqual(
                summary["follow_up"]["repo_map_handoff_reuse"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )
            self.assertEqual(summary["repo_map_handoff_reuse"]["status"], "applied")
            loaded = load_taskpack(work_root / "drafts" / "second-pass")
            items = loaded["backlog"]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(items[0]["input_artifacts"], [taskpack_module.REPO_MAP_HANDOFF_PATH])

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

    def test_agentteam_cli_queue_show_reports_followup_items(self):
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
                    "queue-project",
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

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["queue_status"], "ready")
            self.assertEqual(summary["source_taskpack_id"], "first-pass")
            self.assertTrue(summary["items"])
            self.assertEqual(summary["next_goal"], summary["items"][0]["objective"])
            self.assertIn("agentteam next --from-taskpack first-pass", summary["next_command"])

    def test_agentteam_cli_queue_show_loads_pursue_goal_memory_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop-r2"
            memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "queue-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_taskpack_id": "pursue-loop-r2",
                    "follow_up_queue": [
                        {
                            "objective": "继续修复 queue show 的 pursue recap 路径读取。",
                            "source_taskpack_id": "pursue-loop-r2",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "readiness": "ready",
                        }
                    ],
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 2,
                    "max_rounds": 2,
                    "stop_reason": "max_rounds_reached",
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_report_path": str(run_dir / "reports" / "final_report.md"),
                    "goal_memory_path": str(memory_path),
                    "runs": [{"taskpack_id": "pursue-loop-r2"}],
                },
            )

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "pursue-loop-r2",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["queue_status"], "ready")
            self.assertEqual(summary["selected_item"]["source"], "report.next_steps")
            self.assertTrue(
                any(item["source"] == "goal_memory.follow_up_queue" for item in summary["items"])
            )

    def test_agentteam_cli_queue_show_run_dir_does_not_require_project_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profileless_repo = tmp_path / "profileless-repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "explicit-run"
            _init_repo(profileless_repo)
            _write_completed_operator_run(run_dir)

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(profileless_repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=profileless_repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["queue_status"], "ready")
            self.assertEqual(summary["source_taskpack_id"], "explicit-run")
            self.assertEqual(summary["source_run_dir"], str(run_dir))
            self.assertTrue(summary["items"])

    def test_agentteam_cli_queue_show_run_dir_uses_run_dir_work_root_over_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            profile_work_root = tmp_path / "profile-work-root"
            explicit_work_root = tmp_path / "explicit-work-root"
            run_dir = explicit_work_root / "runs" / "explicit-run"
            memory_path = explicit_work_root / "pursue" / "explicit-goal-memory.json"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, profile_work_root, "queue-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_taskpack_id": "explicit-run",
                    "follow_up_queue": [
                        {
                            "objective": "继续验证显式 run-dir 的 goal memory 读取。",
                            "source_taskpack_id": "explicit-run",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "readiness": "ready",
                        }
                    ],
                },
            )

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["goal_memory_path"], str(memory_path))
            self.assertTrue(
                any(item["source"] == "goal_memory.follow_up_queue" for item in summary["items"])
            )

    def test_agentteam_cli_queue_next_renders_suggested_next_command(self):
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
                    "queue-project",
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

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "next",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            self.assertIn("queue_status: ready", queue_completed.stdout)
            self.assertIn("source_taskpack_id: first-pass", queue_completed.stdout)
            self.assertIn("next_goal:", queue_completed.stdout)
            self.assertIn("next_command: agentteam next --from-taskpack first-pass", queue_completed.stdout)
            self.assertNotIn("status: completed", queue_completed.stdout)

    def test_agentteam_cli_feedback_propose_writes_pending_review_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "feedback-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "first-pass")
            report_path = run_dir / "reports" / "final_report.md"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "feedback",
                    "propose",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                    "--proposal-id",
                    "design-gap-1",
                    "--target-artifact",
                    "design/system.md",
                    "--summary",
                    "实现证据显示系统边界需要补充。",
                    "--rationale",
                    "worker 在实现阶段发现设计文档没有说明 review gate 的归属。",
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
            self.assertEqual(summary["proposal_status"], "pending_review")
            self.assertEqual(summary["source_taskpack_id"], "first-pass")
            proposal_path = Path(summary["proposal_path"])
            self.assertTrue(proposal_path.exists())
            self.assertIn(str(work_root / "semantic_feedback"), str(proposal_path))
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["target_artifacts"], ["design/system.md"])
            self.assertTrue(report_path.exists())

    def test_agentteam_cli_feedback_list_reports_pending_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "feedback-project")
            _write_completed_operator_run(work_root / "runs" / "first-pass")
            propose_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "feedback",
                    "propose",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                    "--proposal-id",
                    "design-gap-1",
                    "--target-artifact",
                    "design/system.md",
                    "--summary",
                    "实现证据显示系统边界需要补充。",
                    "--rationale",
                    "worker 在实现阶段发现设计文档没有说明 review gate 的归属。",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(propose_completed.returncode, 0, propose_completed.stderr)

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "feedback",
                    "list",
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

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            summary = json.loads(list_completed.stdout)
            self.assertEqual(summary["proposal_count"], 1)
            self.assertEqual(summary["proposals"][0]["proposal_id"], "design-gap-1")
            self.assertEqual(summary["proposals"][0]["proposal_status"], "pending_review")

    def test_agentteam_cli_grounding_reports_repo_summary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "grounding-project")
            (repo / "pkg").mkdir()
            (repo / "tests").mkdir()
            (repo / "pkg" / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            (repo / "tests" / "test_module.py").write_text("from pkg.module import run\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'grounding'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "pkg/module.py", "tests/test_module.py", "pyproject.toml"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add grounding files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "grounding",
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
            self.assertEqual(summary["grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(summary["project"], "grounding-project")
            self.assertEqual(summary["scan_status"], "ok")
            self.assertEqual(summary["languages"][0]["language"], "python")
            self.assertEqual(summary["project_tools"][0]["tool_id"], "python-pyproject")
            self.assertEqual(
                summary["candidate_verification_commands"][0]["command"],
                ["python3", "-m", "unittest", "discover"],
            )

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
                    "--expected-authoring-mode",
                    "direct_draft",
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
            for profile in agent_pool["role_runtime_profiles"].values():
                profile["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            for item in backlog["items"]:
                item["write_scope"] = ["generated/"]
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
            self.assertEqual(_implementation_item(loaded["backlog"])["required_role"], "implementation_worker")
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
            task = _implementation_item(loaded["backlog"])
            self.assertEqual(loaded["taskpack"]["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("optimization_candidate_matrix", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_draft_taskpack_canonicalizes_env_python_verification_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            profile = {
                "correctness": {
                    "command": [
                        "env",
                        "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                        "python3",
                        "-m",
                        "unittest",
                        "discover",
                    ]
                }
            }

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a small parser improvement.",
                draft_root=drafts,
                taskpack_id="env-python-profile",
                verification_profile=profile,
            )

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(
                loaded["verification"]["verification_profile"]["correctness"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_apply_verification_profile_canonicalizes_env_python_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            profile = {
                "correctness": {
                    "command": [
                        "env",
                        "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                        "python3",
                        "-m",
                        "unittest",
                        "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                        "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                    ]
                }
            }

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement AgentTeam report diagnostics.",
                draft_root=drafts,
                taskpack_id="codex-profile-apply",
            )
            _apply_verification_profile_to_taskpack(result["taskpack_dir"], profile)

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_deterministic_taskpack_author_materializes_executable_taskpack_from_grounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            tests_dir = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "tests"
            runtime_pkg.mkdir(parents=True)
            tests_dir.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (runtime_pkg / "taskpack_author.py").write_text("def author():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "taskpack.py").write_text("def materialize():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "repo_grounding.py").write_text("def grounding():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "repo_map.py").write_text("def map_repo():\n    return True\n", encoding="utf-8")
            (tests_dir / "test_taskpack.py").write_text("def test_taskpack():\n    assert True\n", encoding="utf-8")
            (tests_dir / "test_m0_runtime.py").write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'agentteam-fixture'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal=(
                    "Feed compact repo_grounding.v1 and repo_structure.v1 signals into "
                    "taskpack authoring and automatic semantic materialization context."
                ),
                draft_root=drafts,
                author_runtime="deterministic",
                taskpack_id="deterministic-author",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            taskpack = loaded["taskpack"]
            task = loaded["backlog"]["items"][0]
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")
            self.assertEqual(taskpack["taskpack_id"], "deterministic-author")
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["semantic_completion"]["authority"], "automatic_deterministic")
            self.assertIn("repo_grounding.v1", task["goal_alignment"])
            self.assertIn("agentteam_target_review_gate", task["required_deliverables"])
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                task["write_scope"],
            )
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            context_refs = taskpack["context_refs"]
            self.assertEqual(context_refs["repo_grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(context_refs["repo_structure_schema_version"], "repo_structure.v1")
            grounding_languages = json.loads(context_refs["repo_grounding_languages"])
            self.assertIn(
                {"language": "python", "file_count": 7},
                [
                    {
                        "language": item["language"],
                        "file_count": item["file_count"],
                    }
                    for item in grounding_languages
                ],
            )
            candidate_commands = json.loads(context_refs["repo_grounding_candidate_verification_commands"])
            self.assertIn(
                ["python3", "-m", "unittest", "discover"],
                [item["command"] for item in candidate_commands],
            )
            repo_structure_budget = json.loads(context_refs["repo_structure_budget"])
            self.assertEqual(repo_structure_budget["omitted_count"], 0)
            self.assertGreaterEqual(repo_structure_budget["included_count"], 3)
            top_level_entries = json.loads(context_refs["repo_structure_top_level_entries"])
            self.assertIn(
                "experiments/",
                [entry["path"] for entry in top_level_entries],
            )
            self.assertEqual(
                taskpack["policy"]["source_control_restrictions"],
                ["no_merge", "no_push", "no_release_activation"],
            )

    def test_deterministic_taskpack_author_prefers_domain_scope_and_records_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            tests_dir = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "tests"
            runtime_pkg.mkdir(parents=True)
            tests_dir.mkdir(parents=True)
            for name in [
                "__init__.py",
                "artifact_lint.py",
                "artifact_repo.py",
                "completion_summary.py",
                "mailbox_worker.py",
                "notifications.py",
                "operator_report.py",
                "taskpack.py",
                "taskpack_author.py",
                "token_usage.py",
                "two_phase_scheduler.py",
                "worker_pool.py",
            ]:
                (runtime_pkg / name).write_text(
                    f"def {name.replace('.', '_')}():\n    return True\n",
                    encoding="utf-8",
                )
            (tests_dir / "test_m0_runtime.py").write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
            (tests_dir / "test_taskpack.py").write_text("def test_taskpack():\n    assert True\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'agentteam-fixture'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal=(
                    "Improve scheduler status report worker heartbeat token usage "
                    "and deterministic scope grounding diagnostics."
                ),
                draft_root=drafts,
                author_runtime="deterministic",
                taskpack_id="deterministic-scope-quality",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            task = loaded["backlog"]["items"][0]
            context_refs = loaded["taskpack"]["context_refs"]
            diagnostic = json.loads(context_refs["deterministic_scope_diagnostic"])

            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/token_usage.py",
                task["write_scope"],
            )
            self.assertNotIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_repo.py",
                task["write_scope"],
            )
            self.assertEqual(diagnostic["confidence"], "high")
            self.assertFalse(diagnostic["authoring_needs_review"])
            self.assertEqual(diagnostic["missing_expected_modules"], [])
            self.assertIn("scheduler", diagnostic["matched_goal_tokens"])
            self.assertIn("heartbeat", diagnostic["matched_goal_tokens"])

    def test_agentteam_cli_taskpack_draft_supports_deterministic_author_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (runtime_pkg / "taskpack_author.py").write_text("def author():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime package"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "draft",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Add deterministic taskpack author runtime.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-deterministic-author",
                    "--author-runtime",
                    "deterministic",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["taskpack_id"], "cli-deterministic-author")
            self.assertEqual(
                validate_taskpack(drafts / "cli-deterministic-author")["status"],
                "accepted",
            )

    def test_codex_taskpack_author_prompt_includes_agentteam_target_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            taskpack_dir = tmp_path / "drafts" / "m49-agentteam-target"
            author_context_dir = tmp_path / "drafts" / ".m49-agentteam-target-author"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")

            prompt = _author_prompt(
                project_root=repo,
                goal="Implement M49 policy hardening for AgentTeam-as-target tasks.",
                taskpack_id="m49-agentteam-target",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("AgentTeam-as-target", prompt)
            self.assertIn("functional or semantic requirement", prompt)
            self.assertIn("do not request git merge or git push", prompt)
            self.assertIn("open-ended improvement requests", prompt)

    def test_codex_taskpack_author_prompt_hardens_decomposition_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "m59-quality"
            author_context_dir = tmp_path / "drafts" / ".m59-quality-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Keep pursuing the operator goal across several bounded implementation rounds.",
                taskpack_id="m59-quality",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("Preserve the operator's original goal", prompt)
            self.assertIn("decompose broad or long-running goals into narrow, measurable next-step tasks", prompt)
            self.assertIn("tie each executable next-step objective to previous evidence", prompt)
            self.assertIn(
                "include a concise rationale naming source_report_path, verification results, blockers, goal_memory_path, or the queue-selected next_goal",
                prompt,
            )
            self.assertIn("avoid safe-but-trivial documentation-only changes unless the operator explicitly asked for documentation", prompt)

    def test_codex_taskpack_author_prompt_requires_role_routed_repo_map_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "role-routing"
            author_context_dir = tmp_path / "drafts" / ".role-routing-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Implement a bounded repository feature.",
                taskpack_id="role-routing",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("repo_map_agent", prompt)
            self.assertIn("implementation_worker", prompt)
            self.assertIn("repo_map_handoff", prompt)
            self.assertIn("depends_on", prompt)
            self.assertIn("risk_target in L0 or L1", prompt)
            self.assertIn("risk_target L2", prompt)
            self.assertIn("risk_target L3", prompt)
            self.assertIn("missing or unclear risk_target as L2", prompt)

    def test_codex_taskpack_author_prompt_requires_broad_framework_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "framework-quality"
            author_context_dir = tmp_path / "drafts" / ".framework-quality-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Strengthen the AgentTeam framework long-run reliability.",
                taskpack_id="framework-quality",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("broad framework enhancement goals", prompt)
            self.assertIn("candidate_changes_or_no_safe_change_rationale", prompt)
            self.assertIn("non_goals", prompt)
            self.assertIn("review_gate", prompt)

    def test_codex_taskpack_author_prompt_includes_roadmap_followup_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "m67-roadmap-followup"
            author_context_dir = tmp_path / "drafts" / ".m67-roadmap-followup-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the roadmap-derived implementation route.\n\n"
                    "Previous taskpack context:\n"
                    "- source_taskpack_id: m67-agentteam-dogfood\n"
                    "- source_report_path: /tmp/work/runs/m67/reports/final_report.md\n"
                    "- selected next_goal: Add taskpack-author route-template guidance.\n"
                ),
                taskpack_id="m67-roadmap-followup",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("Roadmap-derived follow-up task template:", prompt)
            self.assertIn("evidence_paths", prompt)
            self.assertIn("non_goals", prompt)
            self.assertIn("success_metrics_or_no_metric_delta", prompt)
            self.assertIn("roadmap_followup_route_template", prompt)
            self.assertIn("source merge, push, and release activation remain operator review gates", prompt)

    def test_codex_taskpack_author_prompt_uses_direct_artifact_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "direct-author"
            author_context_dir = tmp_path / "drafts" / ".direct-author-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Draft a bounded follow-up taskpack.",
                taskpack_id="direct-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("Direct artifact-production protocol:", prompt)
            self.assertIn("Do not read skill docs", prompt)
            self.assertIn("write the five required taskpack files before optional exploration", prompt)

    def test_codex_taskpack_author_prompt_references_required_file_template_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            author_context_dir = tmp_path / "drafts" / ".direct-author-author"
            taskpack_dir = tmp_path / "drafts" / "direct-author"
            _init_repo(repo)
            author_context_dir.mkdir(parents=True)
            template_path = _write_author_template_bundle(
                author_context_dir=author_context_dir,
                taskpack_id="direct-author",
                project_root=repo,
                goal="Draft a bounded follow-up taskpack.",
                verification_profile={
                    "correctness": {
                        "command": [
                            "env",
                            "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                        ]
                    }
                },
            )

            prompt = _author_prompt(
                project_root=repo,
                goal="Draft a bounded follow-up taskpack.",
                taskpack_id="direct-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
                template_bundle_path=template_path,
            )
            bundle = json.loads(template_path.read_text(encoding="utf-8"))

            self.assertEqual(set(bundle["templates"]), set(REQUIRED_TASKPACK_FILES))
            self.assertEqual(
                bundle["templates"]["verification.json"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertIn("Required file template bundle:", prompt)
            self.assertIn(str(template_path), prompt)
            self.assertIn("replace placeholder values", prompt)

    def test_codex_taskpack_author_bundle_carries_compact_repo_grounding_context_for_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            author_context_dir = tmp_path / "drafts" / ".m67-followup-author"
            taskpack_dir = tmp_path / "drafts" / "m67-followup"
            _init_repo(repo)
            author_context_dir.mkdir(parents=True)
            grounding = {
                "grounding_schema_version": "repo_grounding.v1",
                "scan_status": "ok",
                "tracked_file_count": 42,
                "languages": [
                    {
                        "language": "python",
                        "file_count": 11,
                        "sample_files": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py"],
                    }
                ],
                "project_tools": [
                    {
                        "tool_id": "python-pyproject",
                        "tool_type": "python",
                        "path": "pyproject.toml",
                    }
                ],
                "test_entrypoints": [
                    {
                        "path": "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        "language": "python",
                        "test_framework_hint": "python",
                    }
                ],
                "candidate_verification_commands": [
                    {
                        "command": ["python3", "-m", "unittest", "discover"],
                        "reason": "detected python test files",
                    }
                ],
                "repository_structure": {
                    "structure_schema_version": "repo_structure.v1",
                    "top_level_entry_budget": {
                        "max_entries": 12,
                        "total_entry_count": 3,
                        "included_count": 3,
                        "omitted_count": 0,
                    },
                    "top_level_entries": [
                        {
                            "path": "experiments/",
                            "entry_type": "directory",
                            "file_count": 40,
                        }
                    ],
                },
            }

            template_path = _write_author_template_bundle(
                author_context_dir=author_context_dir,
                taskpack_id="m67-followup",
                project_root=repo,
                goal=(
                    "Follow-up goal: Continue the roadmap-derived implementation route. "
                    "Previous taskpack context: source_report_path=/tmp/work/report.md"
                ),
                repo_grounding=grounding,
            )
            prompt = _author_prompt(
                project_root=repo,
                goal=(
                    "Follow-up goal: Continue the roadmap-derived implementation route. "
                    "Previous taskpack context: source_report_path=/tmp/work/report.md"
                ),
                taskpack_id="m67-followup",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
                template_bundle_path=template_path,
                repo_grounding=grounding,
            )
            bundle = json.loads(template_path.read_text(encoding="utf-8"))

            context = bundle["repo_grounding_context"]
            self.assertEqual(context["repo_grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(context["repo_grounding_scan_status"], "ok")
            self.assertEqual(context["repo_grounding_tracked_file_count"], 42)
            self.assertEqual(
                context["repo_grounding_languages"],
                [{"language": "python", "file_count": 11}],
            )
            self.assertEqual(
                context["repo_grounding_candidate_verification_commands"],
                [
                    {
                        "command": ["python3", "-m", "unittest", "discover"],
                        "reason": "detected python test files",
                    }
                ],
            )
            self.assertEqual(context["repo_structure_schema_version"], "repo_structure.v1")
            self.assertEqual(
                context["repo_structure_top_level_entries"],
                [{"path": "experiments/", "entry_type": "directory", "file_count": 40}],
            )
            self.assertIn("repo_grounding_context", prompt)
            self.assertIn("language, tool, test-entrypoint, and candidate verification-command", prompt)

    def test_codex_taskpack_author_captures_supported_usage_separately_from_estimates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "usage-author"
            author_context_dir = draft_root / ".usage-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("short prompt", encoding="utf-8")
            fake_codex = tmp_path / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, sys",
                        "sys.stdin.read()",
                        "print(json.dumps({",
                        "  'type': 'turn.completed',",
                        "  'usage': {",
                        "    'input_tokens': 101,",
                        "    'cached_input_tokens': 11,",
                        "    'output_tokens': 23,",
                        "    'total_tokens': 124,",
                        "  },",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"

            completed = _run_codex_author_command(
                [str(fake_codex), "exec", "--json"],
                draft_root=draft_root,
                prompt="short prompt",
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="usage-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
                model="test-model",
                author_invocation_context={
                    "project": "usage-project",
                    "pursue_id": "PURSUE-1",
                    "round_index": 2,
                    "usage_stage": "follow_up_author",
                },
                systemd_runner_factory=_AuthorFakeGatedExecution,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            started = json.loads(
                Path(result["model_invocation"]["started_path"]).read_text(
                    encoding="utf-8"
                )
            )
            terminal = result["model_invocation_usage"]
            self.assertEqual(started["usage_stage"], "follow_up_author")
            self.assertEqual(started["round_index"], 2)
            self.assertEqual(started["pursue_id"], "PURSUE-1")
            self.assertEqual(started["coverage_class"], "supported_model_invocation")
            self.assertEqual(terminal["terminal_writer"], "taskpack_author")
            self.assertEqual(terminal["usage_status"], "reported")
            self.assertEqual(terminal["input_tokens"], 101)
            self.assertEqual(terminal["total_tokens"], 124)
            self.assertEqual(result["input_metrics"]["prompt_estimated_tokens"], 3)
            self.assertNotEqual(
                result["input_metrics"]["prompt_estimated_tokens"],
                terminal["input_tokens"],
            )

    def test_author_stage_context_distinguishes_initial_and_follow_up_rounds(self):
        initial = _author_model_invocation_context(
            taskpack_id="round-one",
            draft_root="/tmp/work/drafts",
            model=None,
            supported=False,
            supplied={
                "project": "project",
                "pursue_id": "PURSUE-1",
                "round_index": 1,
                "usage_stage": "taskpack_author",
                "experiment_controller_reference": {
                    "schema_version": (
                        "experiment_budget_controller_reference.v1"
                    )
                },
                "experiment_controller_required": True,
            },
        )
        follow_up = _author_model_invocation_context(
            taskpack_id="round-two",
            draft_root="/tmp/work/drafts",
            model=None,
            supported=False,
            supplied={
                "project": "project",
                "pursue_id": "PURSUE-1",
                "round_index": 2,
                "usage_stage": "follow_up_author",
            },
        )

        self.assertEqual(initial["usage_stage"], "taskpack_author")
        self.assertEqual(initial["round_index"], 1)
        self.assertTrue(initial["experiment_controller_required"])
        self.assertEqual(
            initial["experiment_controller_reference"]["schema_version"],
            "experiment_budget_controller_reference.v1",
        )
        self.assertEqual(follow_up["usage_stage"], "follow_up_author")
        self.assertEqual(follow_up["role"], "follow_up_author")
        self.assertEqual(follow_up["round_index"], 2)

    def test_author_lifecycle_bootstrap_uses_contained_relative_digest_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            authority_root = work_root / "drafts" / ".bootstrap-author"
            context = _author_model_invocation_context(
                taskpack_id="bootstrap",
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={"project": "project"},
            )
            lifecycle = InvocationLifecycle(authority_root, context)
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                terminal_writer="taskpack_author",
            )

            bootstrap = _publish_author_lifecycle_bootstrap(
                work_root / "runs" / "bootstrap",
                work_root,
                {
                    **lifecycle.summary(),
                    "usage_event_id": terminal["usage_event_id"],
                },
            )

            self.assertFalse(Path(bootstrap["started_path"]).is_absolute())
            self.assertFalse(Path(bootstrap["terminal_path"]).is_absolute())
            self.assertEqual(len(bootstrap["started_sha256"]), 64)
            self.assertEqual(len(bootstrap["terminal_sha256"]), 64)
            self.assertEqual(
                bootstrap,
                json.loads(
                    (
                        work_root
                        / "runs"
                        / "bootstrap"
                        / "state"
                        / "author_lifecycle_bootstrap.v1.json"
                    ).read_text(encoding="utf-8")
                ),
            )

    def test_author_bootstrap_import_is_idempotent_and_rejects_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / "runs" / "bootstrap-import"
            authority_root = work_root / "drafts" / ".bootstrap-import-author"
            context = _author_model_invocation_context(
                taskpack_id="bootstrap-import",
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={
                    "project": "project",
                    "run_id": "bootstrap-import",
                },
            )
            lifecycle = InvocationLifecycle(
                authority_root,
                context,
                invocation_id="INV-author-bootstrap-import",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                terminal_writer="taskpack_author",
                finished_at="2026-07-23T00:00:01Z",
            )
            _publish_author_lifecycle_bootstrap(
                run_dir,
                work_root,
                {
                    **lifecycle.summary(),
                    "usage_event_id": terminal["usage_event_id"],
                },
            )

            first = import_author_lifecycle_bootstrap(run_dir)
            second = import_author_lifecycle_bootstrap(run_dir)
            projection = replay_model_invocation_events(
                run_dir / "events.jsonl"
            )

            self.assertEqual(len(first), 2)
            self.assertEqual(second, [])
            self.assertEqual(projection["invocation_count"], 1)
            self.assertEqual(projection["terminal_count"], 1)
            self.assertEqual(projection["open_invocation_ids"], [])
            start_event = next(
                event
                for event in _read_jsonl(run_dir / "events.jsonl")
                if event["event_type"] == "model_invocation_started"
            )
            self.assertEqual(
                start_event["source_event_id"],
                lifecycle.invocation_id,
            )

            bootstrap_path = (
                run_dir
                / "state"
                / "author_lifecycle_bootstrap.v1.json"
            )
            bootstrap = json.loads(
                bootstrap_path.read_text(encoding="utf-8")
            )
            bootstrap["started_sha256"] = "0" * 64
            bootstrap_path.write_text(
                json.dumps(bootstrap, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaises(ModelInvocationIntegrityError):
                import_author_lifecycle_bootstrap(run_dir)

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

    def test_author_recovery_preserves_live_and_reconciles_proven_death_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            live_root = tmp_path / "live-author"
            live_lifecycle = InvocationLifecycle(
                live_root,
                _author_model_invocation_context(
                    taskpack_id="live-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            live_lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            read_fd, write_fd = os.pipe()
            try:
                live = recover_open_author_invocations(
                    live_root,
                    fence_assessor=lambda _start: {
                        "fence_status": "live_pinned",
                        "proof": "pidfd_and_start_ticks_match",
                        "pidfd": read_fd,
                    },
                    service_stopper=lambda _start: False,
                )
            finally:
                os.close(write_fd)
            self.assertEqual(live[0]["reconciliation_status"], "live")
            self.assertFalse(live_lifecycle.terminal_path.exists())

            changed_boot_root = tmp_path / "changed-boot-author"
            changed_boot_lifecycle = InvocationLifecycle(
                changed_boot_root,
                _author_model_invocation_context(
                    taskpack_id="changed-boot-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            changed_boot_lifecycle.publish_start(
                ExecutionGroupIdentity.not_applicable()
            )
            recovered = recover_open_author_invocations(
                changed_boot_root,
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "host_boot_changed",
                },
                service_stopper=lambda _start: False,
            )
            self.assertEqual(
                recovered[0]["reconciliation_status"],
                "recovered",
            )
            terminal = json.loads(
                changed_boot_lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            self.assertEqual(terminal["terminal_status"], "recovered_orphan")
            self.assertEqual(terminal["terminal_writer"], "recovery_controller")
            replay = recover_open_author_invocations(
                changed_boot_root,
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "host_boot_changed",
                },
                service_stopper=lambda _start: False,
            )
            self.assertEqual(
                replay[0]["reconciliation_status"],
                "terminal_available",
            )

            service_root = tmp_path / "service-author"
            service_lifecycle = InvocationLifecycle(
                service_root,
                _author_model_invocation_context(
                    taskpack_id="service-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            service_lifecycle.publish_start(
                ExecutionGroupIdentity.not_applicable()
            )
            assessments = iter(
                [
                    {
                        "fence_status": "exact_service_stop_required",
                        "proof": "exact_transient_cgroup_populated",
                    },
                    {
                        "fence_status": "death_proven",
                        "proof": "exact_transient_cgroup_empty",
                    },
                ]
            )
            stopped = []
            exact = recover_open_author_invocations(
                service_root,
                fence_assessor=lambda _start: next(assessments),
                service_stopper=lambda start: stopped.append(
                    start["invocation_id"]
                )
                or True,
            )
            self.assertEqual(exact[0]["reconciliation_status"], "recovered")
            self.assertEqual(stopped, [service_lifecycle.invocation_id])

    def test_ordinary_taskpack_delete_preserves_author_lifecycle_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            taskpack_id = "preserved-author"
            draft_dir = work_root / "drafts" / taskpack_id
            author_dir = work_root / "drafts" / f".{taskpack_id}-author"
            draft_dir.mkdir(parents=True)
            author_dir.mkdir(parents=True)
            context = _author_model_invocation_context(
                taskpack_id=taskpack_id,
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={"project": "project"},
            )
            lifecycle = InvocationLifecycle(author_dir, context)
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            lifecycle.finalize(
                "failed",
                terminal_writer="taskpack_author",
            )

            deleted = agentteam_module._delete_taskpack_from_profile(
                {"work_root": str(work_root)},
                taskpack_id,
                force=True,
            )

            self.assertEqual(deleted["deleted_count"], 1)
            self.assertFalse(draft_dir.exists())
            self.assertTrue(author_dir.exists())
            self.assertTrue(lifecycle.started_path.exists())
            self.assertTrue(lifecycle.terminal_path.exists())

    def test_codex_taskpack_author_timeout_result_includes_file_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "timeout-author"
            author_context_dir = draft_root / ".timeout-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("prompt", encoding="utf-8")

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time; "
                        "sys.stderr.write('author noise\\n' * 3); "
                        "sys.stderr.flush(); "
                        "time.sleep(5)"
                    ),
                ],
                draft_root=draft_root,
                prompt="",
                timeout_seconds=0.2,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="timeout-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, -9)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertIn("diagnostic", result)
            diagnostic = result["diagnostic"]
            self.assertEqual(diagnostic["required_file_count"], len(REQUIRED_TASKPACK_FILES))
            self.assertEqual(diagnostic["written_required_file_count"], 0)
            self.assertEqual(set(diagnostic["missing_required_files"]), set(REQUIRED_TASKPACK_FILES))
            self.assertEqual(diagnostic["largest_stream"], "stderr")
            self.assertIn("author-direct", diagnostic["next_action"])
            terminal = result["model_invocation_usage"]
            self.assertEqual(terminal["terminal_status"], "timed_out")
            self.assertEqual(terminal["terminal_writer"], "taskpack_author")
            self.assertEqual(terminal["usage_status"], "not_applicable")

    def test_codex_taskpack_author_spools_raw_output_outside_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "spooled-author"
            author_context_dir = draft_root / ".spooled-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("prompt", encoding="utf-8")
            stdout_text = "stdout-line\n" * 700
            stderr_text = "stderr-line\n" * 700

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        f"sys.stdout.write({stdout_text!r}); "
                        f"sys.stderr.write({stderr_text!r}); "
                        "sys.exit(1)"
                    ),
                ],
                draft_root=draft_root,
                prompt="",
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="spooled-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, 1)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("stdout", result)
            self.assertNotIn("stderr", result)
            output = result["output"]
            self.assertEqual(output["stdout_bytes"], len(stdout_text.encode("utf-8")))
            self.assertEqual(output["stderr_bytes"], len(stderr_text.encode("utf-8")))
            self.assertLess(len(output["stdout_excerpt"]), len(stdout_text))
            self.assertLess(len(output["stderr_excerpt"]), len(stderr_text))
            self.assertEqual(Path(output["stdout_path"]).read_text(encoding="utf-8"), stdout_text)
            self.assertEqual(Path(output["stderr_path"]).read_text(encoding="utf-8"), stderr_text)
            self.assertEqual(state["output"]["stdout_path"], output["stdout_path"])
            self.assertEqual(state["output"]["stderr_path"], output["stderr_path"])

    def test_codex_taskpack_author_records_prompt_and_context_input_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "measured-author"
            author_context_dir = draft_root / ".measured-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt = "line one\nline two\n"
            prompt_path.write_text(prompt, encoding="utf-8")
            template_text = json.dumps(
                {"repo_grounding_context": {"repo_grounding_schema_version": "repo_grounding.v1"}},
                sort_keys=True,
            )
            (author_context_dir / "required_file_templates.json").write_text(
                template_text,
                encoding="utf-8",
            )

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.read(); sys.exit(0)",
                ],
                draft_root=draft_root,
                prompt=prompt,
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="measured-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("input_metrics", result)
            self.assertIn("input_metrics", state)
            metrics = result["input_metrics"]
            self.assertEqual(state["input_metrics"], metrics)
            self.assertEqual(metrics["prompt_bytes"], len(prompt.encode("utf-8")))
            self.assertEqual(metrics["prompt_chars"], len(prompt))
            self.assertEqual(metrics["prompt_line_count"], 2)
            self.assertEqual(metrics["prompt_estimated_tokens"], 5)
            self.assertEqual(metrics["author_context_file_count"], 2)
            self.assertEqual(
                metrics["author_context_bytes"],
                len(prompt.encode("utf-8")) + len(template_text.encode("utf-8")),
            )

    def test_codex_taskpack_author_failure_message_includes_compact_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_failed_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import sys",
                        "sys.stderr.write('failure-detail\\n' * 100)",
                        "sys.exit(2)",
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
                    taskpack_id="failed-author",
                    codex_command=[sys.executable, str(fake_codex)],
                    codex_timeout_seconds=5,
                )

            message = str(raised.exception)
            self.assertIn("codex taskpack author failed with exit code 2", message)
            self.assertIn("required_files_written=0/5", message)
            self.assertIn("largest_stream=stderr", message)
            self.assertIn("result_path=", message)
            self.assertIn("state_path=", message)
            self.assertNotIn("failure-detail", message)
            result_path = drafts / ".failed-author-author" / "author_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertIn("output", result)
            self.assertNotIn("stderr", result)
            self.assertEqual(
                result["model_invocation_usage"]["terminal_status"],
                "failed",
            )
            self.assertTrue(
                Path(result["model_invocation"]["started_path"]).is_file()
            )
            self.assertTrue(
                Path(result["model_invocation"]["terminal_path"]).is_file()
            )

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

    def test_draft_taskpack_files_adds_quality_deliverables_for_long_running_followup(self):
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
                    "- source_report_path: /tmp/work/runs/previous/reports/final_report.md\n"
                ),
                draft_root=drafts,
                taskpack_id="followup-quality-deliverables",
                write_scope=["src/runtime.py"],
            )

            loaded = load_taskpack(result["taskpack_dir"])
            deliverables = _implementation_item(loaded["backlog"])["required_deliverables"]
            self.assertIn("roadmap_followup_route_template", deliverables)
            self.assertIn("candidate_changes_or_no_safe_change_rationale", deliverables)
            self.assertIn("non_goals", deliverables)
            self.assertIn("review_gate", deliverables)

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

    def test_canonicalize_codex_taskpack_binds_direct_authoring_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded code change.",
                draft_root=drafts,
                taskpack_id="canonicalize-authoring-mode",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("authoring_mode")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["authoring_mode"], "direct_draft")
            frozen_result = freeze_taskpack(
                result["taskpack_dir"],
                frozen,
                expected_authoring_mode="direct_draft",
            )
            self.assertEqual(
                frozen_result["manifest"]["taskpack_id"],
                "canonicalize-authoring-mode",
            )

    def test_canonicalize_codex_taskpack_without_project_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded code change.",
                draft_root=drafts,
                taskpack_id="missing-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("project_root")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = json.loads(taskpack_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["goal_kind"], "implementation")
            self.assertNotIn("operator_review_required", loaded.get("policy", {}))

    def test_canonicalize_codex_taskpack_preserves_agentteam_target_operator_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement M49 policy hardening for AgentTeam-as-target tasks.",
                draft_root=drafts,
                taskpack_id="m49-agentteam-target",
                read_scope=["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/"],
                write_scope=["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])

            _canonicalize_codex_taskpack_files(taskpack_dir)

            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            task = loaded["backlog"]["items"][0]
            self.assertFalse(taskpack["policy"]["allow_merge"])
            self.assertTrue(taskpack["policy"]["operator_review_required"])
            self.assertEqual(
                taskpack["policy"]["source_control_restrictions"],
                ["no_merge", "no_push", "no_release_activation"],
            )
            self.assertIn("AgentTeam-as-target", task["goal_alignment"])
            self.assertIn("agentteam_target_review_gate", task["required_deliverables"])
            self.assertIn("do not merge", task["objective"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

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

    def test_codex_taskpack_author_preserves_validation_error_for_non_object_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            taskpack_dir = tmp_path / "drafts" / "codex-invalid-taskpack"
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text("[]", encoding="utf-8")
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps({"agents": []}),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps({"command": ["python3", "-m", "unittest", "discover"]}),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(taskpack_dir)
            self.assertIn("taskpack must be an object", str(raised.exception))

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
            implementation_agent = next(
                agent
                for agent in agent_pool["agents"]
                if agent["role"] == "implementation_worker"
            )
            implementation_item = _implementation_item(backlog)
            self.assertEqual(agent_pool["scheduler_agent_id"], "agent-scheduler")
            self.assertEqual(
                implementation_agent["inbox_path"],
                "mailboxes/implementation-worker-1/inbox.jsonl",
            )
            self.assertEqual(
                implementation_agent["outbox_path"],
                "mailboxes/implementation-worker-1/outbox.jsonl",
            )
            self.assertEqual(implementation_item["task_id"], "runtime-optimization-audit-001")
            self.assertEqual(implementation_item["objective"], "Audit and optimize runtime path")
            self.assertEqual(implementation_item["backlog_status"], "ready")
            self.assertEqual(implementation_item["blockers"], [])

    def test_codex_taskpack_author_unwraps_nested_taskpack_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-wrapped-taskpack"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "goal_kind": "implementation",
                        "semantic_contract_version": "task_semantics.v1",
                        "taskpack": {
                            "taskpack_schema_version": "taskpack.v1",
                            "taskpack_id": "codex-wrapped-taskpack",
                            "status": "draft",
                            "semantic_contract_version": "task_semantics.v1",
                            "project_root": str(repo),
                            "goal": "Implement a bounded runtime improvement.",
                            "original_goal": "Implement a bounded runtime improvement.",
                            "goal_kind": "implementation",
                            "runtime": {"default_backend": "codex"},
                            "files": {
                                "agent_pool": "agent_pool.json",
                                "backlog": "backlog.json",
                                "verification": "verification.json",
                            },
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
                                "item_id": "wrapped-001",
                                "title": "Implement bounded runtime improvement.",
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
            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertEqual(taskpack["taskpack_id"], "codex-wrapped-taskpack")
            self.assertNotIn("taskpack", taskpack)

    def test_classify_goal_kind_treats_generic_improve_as_implementation(self):
        self.assertEqual(
            taskpack_module.classify_goal_kind("Improve repo grounding with deterministic structure signals."),
            "implementation",
        )
        self.assertEqual(
            taskpack_module.classify_goal_kind("改进任务包语义补全流程。"),
            "implementation",
        )
        self.assertEqual(
            taskpack_module.classify_goal_kind("Improve parser latency with a benchmarked optimization."),
            "optimization",
        )

    def test_canonicalize_codex_taskpack_preserves_declared_implementation_for_generic_improve_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-generic-improve"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            goal = "Improve repo grounding with deterministic structure signals."
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-generic-improve",
                        "status": "draft",
                        "semantic_contract_version": "task_semantics.v1",
                        "project_root": str(repo),
                        "goal": goal,
                        "original_goal": goal,
                        "goal_kind": "implementation",
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
                                "item_id": "generic-improve-001",
                                "title": "Implement deterministic grounding signals.",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["README.md"],
                                "write_scope": ["src/grounding.py"],
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
            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertEqual(taskpack["goal_kind"], "implementation")

    def test_codex_taskpack_author_unwraps_nested_verification_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-wrapped-verification"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-wrapped-verification",
                        "status": "draft",
                        "semantic_contract_version": "task_semantics.v1",
                        "project_root": str(repo),
                        "goal": "Implement a bounded runtime feature.",
                        "original_goal": "Implement a bounded runtime feature.",
                        "goal_kind": "implementation",
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
                                "item_id": "wrapped-verification-001",
                                "title": "Implement bounded runtime feature.",
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
                json.dumps(
                    {
                        "verification": {
                            "command": ["python3", "-m", "unittest", "discover"],
                            "expected_evidence": ["exit code"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(verification["command"], ["python3", "-m", "unittest", "discover"])
            self.assertNotIn("verification", verification)

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
            task = _implementation_item(backlog)
            self.assertEqual(taskpack["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_canonicalizes_optimization_audit_to_code_investigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-optimization-audit"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-optimization-audit",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "优化 AgentTeam 长期运行状态提示。",
                        "goal_kind": "optimization",
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
                                "task_id": "optimize-progress-breadcrumb",
                                "objective": (
                                    "Audit progress breadcrumb output and implement a narrow "
                                    "code/test fix if a concrete ambiguity is found."
                                ),
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "work_type": "audit",
                                "goal_alignment": (
                                    "Ties directly to previous evidence and the queue-selected next_goal."
                                ),
                                "read_scope": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py"],
                                "write_scope": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py"],
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

            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            self.assertEqual(task["work_type"], "code_investigation")
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

    def test_codex_taskpack_author_accepts_valid_complete_draft_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            taskpack_dir = drafts / "timeout-complete-draft"
            fake_codex = tmp_path / "fake_timeout_complete_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import sys",
                        "import time",
                        "repo = pathlib.Path(sys.argv[1]).resolve()",
                        "taskpack_dir = pathlib.Path(sys.argv[2]).resolve()",
                        "taskpack_id = taskpack_dir.name",
                        "goal = 'Improve fixture behavior.'",
                        "taskpack = {",
                        "    'taskpack_schema_version': 'taskpack.v1',",
                        "    'taskpack_id': taskpack_id,",
                        "    'status': 'draft',",
                        "    'semantic_contract_version': 'task_semantics.v1',",
                        "    'project_root': str(repo),",
                        "    'goal': goal,",
                        "    'original_goal': goal,",
                        "    'goal_kind': 'implementation',",
                        "    'runtime': {'default_backend': 'codex'},",
                        (
                            "    'files': {'agent_pool': 'agent_pool.json', "
                            "'backlog': 'backlog.json', 'verification': 'verification.json'},"
                        ),
                        "    'policy': {'allow_merge': False},",
                        "}",
                        "agent_pool = {",
                        "    'scheduler_agent_id': 'agent-scheduler',",
                        "    'role_runtime_profiles': {'implementation_worker': {'adapter': 'codex'}},",
                        "    'agents': [{",
                        "        'agent_id': 'agent-implementation-worker-1',",
                        "        'role': 'implementation_worker',",
                        "        'status': 'idle',",
                        "        'inbox_path': 'mailboxes/agent-implementation-worker-1/inbox.jsonl',",
                        "        'outbox_path': 'mailboxes/agent-implementation-worker-1/outbox.jsonl',",
                        "    }],",
                        "}",
                        "backlog = {'backlog_id': 'BL-timeout-complete-draft', 'items': [{",
                        "    'task_id': 'TASK-TIMEOUT-COMPLETE-DRAFT-001',",
                        "    'objective': 'Improve fixture behavior.',",
                        "    'goal_alignment': 'Preserves the original goal: Improve fixture behavior.',",
                        "    'work_type': 'code_implementation',",
                        "    'required_deliverables': ['verification_summary'],",
                        "    'backlog_status': 'ready',",
                        "    'risk_target': 'L1',",
                        "    'depends_on': [],",
                        "    'read_scope': ['README.md'],",
                        "    'write_scope': ['README.md'],",
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
                        "(taskpack_dir / 'README.md').write_text('# timeout complete draft\\n', encoding='utf-8')",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="codex",
                taskpack_id="timeout-complete-draft",
                codex_command=["python3", str(fake_codex), str(repo), str(taskpack_dir)],
                codex_timeout_seconds=0.2,
            )

            self.assertTrue(result["author_timeout_salvaged"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")
            result_path = drafts / ".timeout-complete-draft-author" / "author_result.json"
            state_path = drafts / ".timeout-complete-draft-author" / "author_state.json"
            author_result = json.loads(result_path.read_text(encoding="utf-8"))
            author_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(author_result["status"], "accepted_after_timeout")
            self.assertEqual(author_state["author_status"], "accepted_after_timeout")
            self.assertTrue(author_result["salvage"]["accepted"])

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
            task = _implementation_item(loaded["backlog"])
            self.assertIn("goal_alignment", task)
            self.assertIn("required_deliverables", task)
            self.assertIn("verification_summary", task["required_deliverables"])
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            self.assertEqual(task["write_scope"], ["src/"])

    def test_deterministic_taskpack_skeleton_keeps_uncertain_semantics_as_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="deterministic-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                    "selected_next_goal": "Investigate the next implementation step.",
                },
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            taskpack_dir = Path(result["taskpack_dir"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]

            self.assertTrue(taskpack["semantic_authoring_required"])
            self.assertEqual(taskpack["authoring_mode"], "deterministic_skeleton")
            self.assertEqual(
                taskpack["context_refs"]["repo_map_manifest_path"],
                "/tmp/work/state/repo_map/manifest.json",
            )
            self.assertEqual(item["work_type"], "code_investigation")
            self.assertTrue(item["semantic_authoring_required"])
            self.assertEqual(item["write_scope"], [".agentteam/generated/"])
            self.assertIn("semantic_slots", item)
            self.assertIn("task_specific_objective", item["semantic_slots"])
            self.assertNotIn("optimization_candidate_matrix", item["required_deliverables"])
            self.assertNotIn("agentteam/**", item["write_scope"])

    def test_deterministic_taskpack_skeleton_rejects_high_semantic_goals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_deterministic_taskpack_skeleton(
                    project_root=repo,
                    goal="Optimize the existing competition repository latency.",
                    draft_root=drafts,
                    taskpack_id="deterministic-optimization",
                    context_refs={"repo_map_manifest_path": "/tmp/repo-map/manifest.json"},
                )

            self.assertIn("requires semantic authoring", str(raised.exception))

    def test_materialize_semantic_taskpack_turns_skeleton_into_runtime_launchable_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="semantic-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                },
            )

            materialized = taskpack_module.materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="semantic-executable",
                semantic_task={
                    "objective": "Implement the bounded parser cache fix described by the source report.",
                    "goal_alignment": "Uses source_report_path evidence to select one bounded implementation change.",
                    "read_scope": ["src/", "tests/"],
                    "write_scope": ["src/parser.py", "tests/test_parser.py"],
                    "work_type": "code_implementation",
                    "required_deliverables": [
                        "implemented_changes_or_no_safe_change_rationale",
                        "verification_summary",
                        "recommended_next_implementation_tasks",
                    ],
                    "verification_command": ["python3", "-m", "unittest", "discover"],
                    "evidence_paths": ["/tmp/work/runs/previous/reports/final_report.md"],
                },
            )

            taskpack_dir = Path(materialized["taskpack_dir"])
            validation = validate_taskpack(taskpack_dir)
            self.assertEqual(validation["status"], "accepted")
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertEqual(taskpack["materialized_from"]["taskpack_id"], "semantic-skeleton")
            self.assertFalse(item.get("semantic_authoring_required"))
            self.assertEqual(item["blockers"], [])
            self.assertEqual(item["objective"], "Implement the bounded parser cache fix described by the source report.")
            self.assertEqual(item["write_scope"], ["src/parser.py", "tests/test_parser.py"])
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--backlog"), str(Path(frozen["frozen_taskpack_dir"]) / "backlog.json"))
            self.assertTrue((run_root / "semantic-executable").exists())

    def test_auto_materialize_semantic_taskpack_completes_roadmap_skeleton_without_operator_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            (repo / "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime").mkdir(parents=True)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the roadmap-derived implementation route.\n\n"
                    "Previous taskpack context:\n"
                    "- source_report_path: /tmp/work/runs/previous/reports/final_report.md\n\n"
                    "Instructions for the new taskpack:\n"
                    "- Use the queue-selected next_goal to produce an executable taskpack."
                ),
                draft_root=drafts,
                taskpack_id="auto-roadmap-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_context_path": "/tmp/work/repo_contexts/implementation_worker.json",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                    "selected_next_goal": "Implement automatic semantic completion authoring path.",
                    "read_scope": "\n".join(
                        [
                            "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                            "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        ]
                    ),
                    "write_scope": "\n".join(
                        [
                            "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                            "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        ]
                    ),
                    "non_goals": "merge, push, release activation",
                },
            )

            materialized = taskpack_module.auto_materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="auto-roadmap-executable",
            )

            taskpack_dir = Path(materialized["taskpack_dir"])
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertEqual(taskpack["semantic_completion"]["authority"], "automatic_deterministic")
            self.assertIs(taskpack["semantic_completion"]["operator_semantic_json_required"], False)
            self.assertIn("source_report_path", item["objective"])
            self.assertIn("queue-selected next_goal", item["objective"])
            self.assertEqual(
                item["read_scope"],
                [
                    "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                    "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                ],
            )
            self.assertEqual(
                item["write_scope"],
                [
                    "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                    "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                ],
            )
            self.assertIn("roadmap_followup_route_template", item["required_deliverables"])
            self.assertIn("agentteam_target_review_gate", item["required_deliverables"])
            self.assertIn(
                "/tmp/work/runs/previous/reports/final_report.md",
                item["semantic_materialization"]["evidence_paths"],
            )
            self.assertEqual(item["blockers"], [])

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--backlog"), str(Path(frozen["frozen_taskpack_dir"]) / "backlog.json"))
            self.assertTrue((run_root / "auto-roadmap-executable").exists())

    def test_auto_materialize_semantic_taskpack_uses_grounding_candidate_verification_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            _init_repo(repo)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Use deterministic grounding to produce an executable follow-up taskpack.",
                draft_root=drafts,
                taskpack_id="candidate-verification-skeleton",
                context_refs={
                    "selected_next_goal": "Implement the bounded parser task.",
                    "read_scope": "src/parser.py\ntests/test_parser.py",
                    "write_scope": "src/parser.py\ntests/test_parser.py",
                    "repo_grounding_candidate_verification_commands": json.dumps(
                        [
                            {
                                "command": ["python3", "-m", "pytest", "tests/test_parser.py"],
                                "reason": "detected focused python test entrypoint",
                            }
                        ]
                    ),
                },
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            materialized = taskpack_module.auto_materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="candidate-verification-executable",
            )

            loaded = load_taskpack(materialized["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                ["python3", "-m", "pytest", "tests/test_parser.py"],
            )
            self.assertEqual(validate_taskpack(materialized["taskpack_dir"])["status"], "accepted")

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

    def test_validate_taskpack_rejects_invalid_artifact_fields_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task artifact fields.",
                draft_root=drafts,
                taskpack_id="malformed-artifact-fields",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            repo_map_item = backlog["items"][0]
            implementation_item = _implementation_item(backlog)
            repo_map_item["expected_output_artifacts"] = ["../outside.json"]
            implementation_item["input_artifacts"] = ".agentteam/generated/repo_map_handoff.json"
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("expected_output_artifacts", message)
            self.assertIn("input_artifacts", message)

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

    def test_build_taskpack_runtime_args_passes_codex_model_from_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with a worker model.",
                draft_root=drafts,
                taskpack_id="codex-model-runtime-args",
                write_scope=["src/"],
                codex_timeout_seconds=123,
                codex_model="medium",
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--codex-model"), "medium")
            self.assertEqual(_arg_value(args, "--codex-timeout-seconds"), "123")

    def test_draft_taskpack_routes_implementation_through_repo_map_then_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded feature in the repository.",
                draft_root=drafts,
                taskpack_id="role-routed-implementation",
                write_scope=["src/"],
            )

            loaded = load_taskpack(result["taskpack_dir"])
            agent_roles = {
                agent["role"]
                for agent in loaded["agent_pool"]["agents"]
            }
            items = loaded["backlog"]["items"]
            handoff_path = ".agentteam/generated/repo_map_handoff.json"

            self.assertIn("repo_map_agent", agent_roles)
            self.assertIn("implementation_worker", agent_roles)
            self.assertEqual([item["required_role"] for item in items], [
                "repo_map_agent",
                "implementation_worker",
            ])
            self.assertEqual(items[0]["work_type"], "repository_mapping")
            self.assertEqual(items[0]["write_scope"], [".agentteam/generated/"])
            self.assertEqual(items[0]["expected_output_artifacts"], [handoff_path])
            self.assertEqual(items[1]["depends_on"], [items[0]["task_id"]])
            self.assertEqual(items[1]["input_artifacts"], [handoff_path])
            self.assertEqual(items[1]["risk_target"], "L2")
            self.assertIn("repo_map_handoff", items[0]["required_deliverables"])
            self.assertIn("repo_map_handoff", items[1]["required_deliverables"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_draft_taskpack_skips_repo_map_for_l1_implementation_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded local code change.",
                draft_root=drafts,
                taskpack_id="l1-direct-implementation",
                write_scope=["src/"],
                risk_target="L1",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            agent_roles = [agent["role"] for agent in loaded["agent_pool"]["agents"]]
            items = loaded["backlog"]["items"]

            self.assertEqual(agent_roles, ["implementation_worker"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["risk_target"], "L1")
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0].get("input_artifacts"), None)
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

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

    def test_reuse_repo_map_handoff_in_taskpack_removes_repo_map_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement the next bounded feature in the repository.",
                draft_root=drafts,
                taskpack_id="reuse-repo-map-handoff",
                write_scope=["src/"],
            )

            reuse = taskpack_module.reuse_repo_map_handoff_in_taskpack(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            roles = [agent["role"] for agent in loaded["agent_pool"]["agents"]]
            items = loaded["backlog"]["items"]
            handoff_path = taskpack_module.REPO_MAP_HANDOFF_PATH

            self.assertEqual(reuse["status"], "applied")
            self.assertEqual(reuse["handoff_path"], handoff_path)
            self.assertEqual(reuse["removed_task_count"], 1)
            self.assertNotIn("repo_map_agent", roles)
            self.assertEqual(roles, ["implementation_worker"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(items[0]["input_artifacts"], [handoff_path])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_followup_detects_reusable_repo_map_handoff_from_integration_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            baseline = tmp_path / "integration-baseline"
            handoff = baseline / taskpack_module.REPO_MAP_HANDOFF_PATH
            _write_json(handoff, {"schema_version": "repo_map_handoff.v1"})
            source_report = {
                "integration_baseline": {
                    "worktree_path": str(baseline),
                    "worktree_exists": True,
                    "branch": "agentteam/run/first-pass/integration",
                }
            }

            reuse = agentteam_module._reusable_repo_map_handoff_path(source_report)

            self.assertEqual(reuse, taskpack_module.REPO_MAP_HANDOFF_PATH)

    def test_build_taskpack_runtime_args_passes_initial_integration_base_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with inherited integration baseline.",
                draft_root=drafts,
                taskpack_id="runtime-args-inherited-baseline",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                initial_integration_base_ref="abc123",
            )

            self.assertEqual(_arg_value(args, "--initial-integration-base-ref"), "abc123")

    def test_pursue_next_integration_base_ref_prefers_latest_baseline_head(self):
        report = {
            "integration_baseline": {
                "head_sha": "verified-head",
                "branch": "agentteam/run/previous/integration",
            }
        }

        self.assertEqual(_pursue_next_integration_base_ref(report), "verified-head")

    def test_pursue_next_integration_base_ref_falls_back_to_branch(self):
        report = {
            "integration_baseline": {
                "head_sha": None,
                "branch": "agentteam/run/previous/integration",
            }
        }

        self.assertEqual(
            _pursue_next_integration_base_ref(report),
            "agentteam/run/previous/integration",
        )

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

    def test_build_taskpack_runtime_args_rejects_semantic_skeleton_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="runtime-semantic-skeleton",
                context_refs={"source_report_path": "/tmp/work/report.md"},
            )
            frozen = freeze_taskpack(skeleton["taskpack_dir"], frozen_root)

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertIn("semantic authoring required before runtime launch", str(raised.exception))
            self.assertFalse((run_root / "runtime-semantic-skeleton").exists())

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

    def _publish_pre04_run(self, work_root, release, run_id, sequence_project="pre04"):
        return publish_implementation_run(
            work_root,
            project_key=sequence_project,
            run_id=run_id,
            taskpack_id=run_id,
            release_identity=release,
        )

    def test_pre04_01_active_switch_keeps_bound_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            first = _pre04_release_fixture(work_root, "release-1")
            second = _pre04_release_fixture(work_root, "release-2", "2" * 40)
            pair = self._publish_pre04_run(work_root, first, "run-1")
            _write_json(work_root / "releases" / "active.json", second)

            validated = validate_run_binding(pair["run_dir"], expected_project_key="pre04")

            self.assertEqual(validated["binding"]["release_id"], "release-1")

    def test_pre04_02_publish_uses_already_selected_release_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            selected = _pre04_release_fixture(work_root, "release-1")
            active = _pre04_release_fixture(work_root, "release-2", "2" * 40)
            _write_json(work_root / "releases" / "active.json", active)

            pair = self._publish_pre04_run(work_root, selected, "run-1")

            self.assertEqual(pair["binding"]["release_id"], "release-1")

    def test_pre04_03_tampered_or_missing_bound_release_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            pair = self._publish_pre04_run(work_root, release, "run-1")
            binding_path = Path(pair["run_dir"]) / "state" / "runtime_release_binding.v1.json"
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["source_commit"] = "f" * 40
            _write_json(binding_path, binding)

            with self.assertRaises(AgentTeamReleaseError):
                validate_run_binding(pair["run_dir"], expected_project_key="pre04")

    def test_pre04_04_bound_terminal_release_is_gc_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            bound = _pre04_release_fixture(work_root, "release-1")
            _pre04_release_fixture(work_root, "release-2", "2" * 40)
            self._publish_pre04_run(work_root, bound, "run-1")

            result = prune_releases(work_root, keep_latest=0)

            self.assertIn("release-1", result["protected_release_ids"])
            self.assertTrue(Path(bound["release_root"]).exists())

    def test_pre04_05_approval_expected_release_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")

            with self.assertRaises(AgentTeamReleaseError):
                publish_implementation_run(
                    work_root,
                    project_key="pre04",
                    run_id="run-1",
                    taskpack_id="run-1",
                    release_identity=release,
                    expected_release={"release_id": "release-2"},
                )

    def test_pre04_06_legacy_run_is_excluded_from_implicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            legacy = work_root / "runs" / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "events.jsonl").write_text("", encoding="utf-8")

            with self.assertRaises(AgentTeamReleaseError):
                select_latest_implementation_run(work_root, expected_project_key="pre04")
            self.assertTrue(legacy.is_dir())

    def test_pre04_07_explicit_and_implicit_selection_share_identity_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            explicit = self._publish_pre04_run(work_root, release, "run-1")

            implicit = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(explicit["identity_sha256"], implicit["identity_sha256"])
            self.assertEqual(explicit["binding"], implicit["binding"])

    def test_pre04_08_initial_run_launcher_resolves_frozen_expected_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            _init_repo(repo)
            release = _pre04_release_fixture(
                work_root,
                "release-1",
                _git_head(repo),
                runtime_source=Path(__file__).resolve().parents[1],
            )
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise initial immutable runtime binding.",
                draft_root=work_root / "drafts",
                taskpack_id="run-1",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(draft["taskpack_dir"], "fake")
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen_result = freeze_taskpack(draft["taskpack_dir"], work_root / "frozen")
            frozen = Path(frozen_result["frozen_taskpack_dir"])
            launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

            selection = launcher["_initial_run_selection"](
                ["run", str(frozen), "--run-root", str(work_root / "runs")]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[4] / "agentteam"),
                    "run",
                    str(frozen),
                    "--run-root",
                    str(work_root / "runs"),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(selection["release"]["release_id"], "release-1")
            self.assertEqual(selection["run_dir"], str(work_root / "runs" / "run-1"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            bound = validate_run_binding(
                work_root / "runs" / "run-1", expected_project_key="pre04"
            )
            self.assertEqual(bound["binding"]["release_id"], "release-1")

    def test_pre04_08b_launcher_supports_versioned_artifact_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            _init_repo(repo)
            release = _pre04_release_fixture(
                work_root,
                "release-1",
                _git_head(repo),
                runtime_source=Path(__file__).resolve().parents[1],
            )
            older = publish_implementation_run(
                work_root,
                project_key="pre04-versioned",
                run_id="older-run",
                taskpack_id="run-1",
                release_identity=release,
            )
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04-versioned",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise versioned immutable runtime binding.",
                draft_root=work_root / "drafts" / "v2",
                taskpack_id="run-1",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(
                draft["taskpack_dir"],
                "fake",
            )
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(
                taskpack_path.read_text(encoding="utf-8")
            )
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen_result = freeze_taskpack(
                draft["taskpack_dir"],
                work_root / "frozen" / "v2",
            )
            frozen = Path(frozen_result["frozen_taskpack_dir"])
            flat_frozen = Path(
                freeze_taskpack(
                    draft["taskpack_dir"],
                    work_root / "frozen",
                )["frozen_taskpack_dir"]
            )
            run_root = work_root / "runs" / "v2"
            launcher = runpy.run_path(
                str(Path(__file__).resolve().parents[4] / "agentteam")
            )

            for mismatched_frozen, mismatched_run_root in (
                (frozen, work_root / "runs"),
                (frozen, work_root / "runs" / "v3"),
                (flat_frozen, work_root / "runs" / "v2"),
            ):
                with self.assertRaises(launcher["LauncherError"]):
                    launcher["_initial_run_selection"](
                        [
                            "run",
                            str(mismatched_frozen),
                            "--run-root",
                            str(mismatched_run_root),
                        ]
                    )
            selection = launcher["_initial_run_selection"](
                [
                    "run",
                    str(frozen),
                    "--run-root",
                    str(run_root),
                ]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[4] / "agentteam"),
                    "run",
                    str(frozen),
                    "--run-root",
                    str(run_root),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(selection["work_root"], str(work_root))
            self.assertEqual(selection["release"]["release_id"], "release-1")
            self.assertEqual(
                selection["run_dir"],
                str(run_root / "run-1"),
            )
            bound = validate_run_binding(
                run_root / "run-1",
                expected_project_key="pre04-versioned",
            )
            self.assertEqual(bound["binding"]["release_id"], "release-1")
            latest = select_latest_implementation_run(
                work_root,
                expected_project_key="pre04-versioned",
            )
            self.assertEqual(
                validate_run_binding(
                    older["run_dir"],
                    expected_project_key="pre04-versioned",
                )["identity"]["creation_sequence"],
                1,
            )
            self.assertEqual(latest["identity"]["creation_sequence"], 2)
            self.assertEqual(
                latest["run_dir"],
                str(run_root / "run-1"),
            )
            explicit_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_root / "run-1"),
                ],
                "continue",
            )
            taskpack_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "run-1",
                ],
                "continue",
            )
            latest_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                ],
                "continue",
            )
            for resume_selection in (
                explicit_selection,
                taskpack_selection,
                latest_selection,
            ):
                self.assertEqual(
                    resume_selection["run_dir"],
                    str(run_root / "run-1"),
                )
                self.assertEqual(
                    resume_selection["frozen_taskpack_dir"],
                    str(frozen),
                )

            for resume_args in (
                [
                    "--run-dir",
                    str(run_root / "run-1"),
                ],
                [
                    "--taskpack",
                    "run-1",
                ],
                [],
            ):
                continued = subprocess.run(
                    [
                        str(Path(__file__).resolve().parents[4] / "agentteam"),
                        "continue",
                        "--project-root",
                        str(repo),
                        *resume_args,
                        "--one-shot",
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(continued.returncode, 0, continued.stderr)
                continue_result = json.loads(continued.stdout)
                self.assertEqual(
                    continue_result["paths"]["run_dir"],
                    str(run_root / "run-1"),
                )
                self.assertEqual(
                    continue_result["paths"]["frozen_taskpack_dir"],
                    str(frozen),
                )
            with self.assertRaises(launcher["LauncherError"]):
                launcher["_initial_run_selection"](
                    [
                        "run",
                        str(frozen),
                        "--run-root",
                        str(tmp_path / "other-work" / "runs" / "v2"),
                    ]
                )

    def test_pre04_08c_flat_vn_run_id_is_not_treated_as_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            _init_repo(repo)
            release = _pre04_release_fixture(
                work_root,
                "release-1",
                _git_head(repo),
                runtime_source=Path(__file__).resolve().parents[1],
            )
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise a flat vN run identity.",
                draft_root=work_root / "drafts",
                taskpack_id="v2",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(draft["taskpack_dir"], "fake")
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen = Path(
                freeze_taskpack(
                    draft["taskpack_dir"],
                    work_root / "frozen",
                )["frozen_taskpack_dir"]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            launcher_path = str(Path(__file__).resolve().parents[4] / "agentteam")
            started = subprocess.run(
                [
                    launcher_path,
                    "run",
                    str(frozen),
                    "--run-root",
                    str(work_root / "runs"),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            pair = validate_run_binding(
                work_root / "runs" / "v2",
                expected_project_key="pre04",
            )
            launcher = runpy.run_path(
                launcher_path
            )

            runtime_selected = select_latest_implementation_run(
                work_root,
                expected_project_key="pre04",
            )
            launcher_selected = launcher["_latest_bound_run"](
                work_root,
                "pre04",
            )

            self.assertEqual(runtime_selected["run_dir"], pair["run_dir"])
            self.assertEqual(launcher_selected["run_dir"], pair["run_dir"])
            for resume_args in (
                ["--run-dir", pair["run_dir"]],
                ["--taskpack", "v2"],
                [],
            ):
                continued = subprocess.run(
                    [
                        launcher_path,
                        "continue",
                        "--project-root",
                        str(repo),
                        *resume_args,
                        "--one-shot",
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(continued.returncode, 0, continued.stderr)
                result = json.loads(continued.stdout)
                self.assertEqual(result["paths"]["run_dir"], pair["run_dir"])
                self.assertEqual(
                    result["paths"]["frozen_taskpack_dir"],
                    str(frozen),
                )
            with self.assertRaises(AgentTeamReleaseError):
                publish_implementation_run(
                    work_root,
                    project_key="pre04",
                    run_id="nested-run",
                    taskpack_id="nested-run",
                    release_identity=release,
                    run_root=work_root / "runs" / "v2",
                )

    def test_pre04_09_acceptance_evidence_does_not_replace_latest_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            implementation = self._publish_pre04_run(work_root, release, "run-1")
            publish_acceptance_run_identity(
                work_root,
                project_key="pre04",
                run_id="evidence-1",
                taskpack_id="gate-taskpack",
                implementation_run_id="run-1",
                gate_epoch=1,
            )

            latest = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(latest["run_dir"], implementation["run_dir"])

    def test_pre04_10_path_installed_launcher_uses_bound_release_after_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            bin_dir = tmp_path / "bin"
            _init_repo(repo)
            bin_dir.mkdir()
            launcher_source = Path(__file__).resolve().parents[4] / "agentteam"
            installed = bin_dir / "agentteam"
            shutil.copy2(launcher_source, installed)
            installed.chmod(0o755)
            runtime_source = Path(__file__).resolve().parents[1]
            first = _pre04_release_fixture(
                work_root, "release-1", runtime_source=runtime_source
            )
            second = _pre04_release_fixture(
                work_root, "release-2", "2" * 40, runtime_source=runtime_source
            )
            pair = self._publish_pre04_run(work_root, first, "run-1")
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                ),
            )
            _write_json(work_root / "releases" / "active.json", second)
            _write_json(
                Path(pair["run_dir"]) / "state" / "scheduler_state.json",
                {"scheduler_status": "completed"},
            )
            env = _test_env()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"

            completed = subprocess.run(
                [
                    shutil.which("agentteam", path=env["PATH"]),
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    pair["run_dir"],
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(shutil.which("agentteam", path=env["PATH"]), str(installed))
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_pre04_11_concurrent_creation_allocates_unique_monotonic_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            results = []
            errors = []

            def create(run_id):
                try:
                    results.append(self._publish_pre04_run(work_root, release, run_id))
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=create, args=(f"run-{index}",))
                for index in range(1, 5)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(item["identity"]["creation_sequence"] for item in results),
                [1, 2, 3, 4],
            )

    def test_pre04_12_mtime_and_evidence_creation_do_not_change_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            older = self._publish_pre04_run(work_root, release, "z-old")
            newer = self._publish_pre04_run(work_root, release, "a-new")
            os.utime(older["run_dir"], (2000000000, 2000000000))
            publish_acceptance_run_identity(
                work_root,
                project_key="pre04",
                run_id="evidence-new",
                taskpack_id="gate-taskpack",
                implementation_run_id="a-new",
                gate_epoch=2,
            )

            latest = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(latest["run_dir"], newer["run_dir"])

    def test_pre04_13_newest_failed_run_remains_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            self._publish_pre04_run(work_root, release, "run-old")
            newest = self._publish_pre04_run(work_root, release, "run-failed")
            _write_json(
                Path(newest["run_dir"]) / "state" / "scheduler_state.json",
                {"scheduler_status": "failed"},
            )

            selected = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(selected["run_dir"], newest["run_dir"])

    def test_pre04_14_explicit_older_bound_run_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            older = self._publish_pre04_run(work_root, release, "run-old")
            self._publish_pre04_run(work_root, release, "run-new")

            explicit = validate_run_binding(
                older["run_dir"], expected_project_key="pre04"
            )

            self.assertEqual(explicit["identity"]["creation_sequence"], 1)

    def test_pre04_15_legacy_run_can_be_adopted_without_blocking_new_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            legacy = work_root / "runs" / "legacy-run"
            legacy.mkdir(parents=True)
            marker = legacy / "preserved.txt"
            marker.write_text("legacy evidence\n", encoding="utf-8")

            created = self._publish_pre04_run(work_root, release, "new-run")
            with self.assertRaises(AgentTeamReleaseError):
                select_latest_implementation_run(work_root, expected_project_key="pre04")

            adopted = adopt_legacy_implementation_run(
                work_root,
                project_key="pre04",
                run_id="legacy-run",
                taskpack_id="legacy-run",
                release_identity=release,
            )

            self.assertEqual(created["identity"]["creation_sequence"], 1)
            self.assertEqual(adopted["identity"]["creation_sequence"], 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "legacy evidence\n")
            self.assertEqual(
                select_latest_implementation_run(
                    work_root,
                    expected_project_key="pre04",
                )["run_dir"],
                str(legacy.resolve()),
            )

    def test_pre04_16_update_command_adopts_only_with_explicit_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "work"
            _init_repo(repo)
            release = _pre04_release_fixture(work_root, "release-1")
            _write_json(work_root / "releases" / "active.json", release)
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                ),
            )
            (work_root / "runs" / "legacy-run").mkdir(parents=True)

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rejected = agentteam_module.main(
                    [
                        "update",
                        "--project-root",
                        str(repo),
                        "--adopt-run",
                        "legacy-run",
                        "--json",
                    ]
                )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                adopted = agentteam_module.main(
                    [
                        "update",
                        "--project-root",
                        str(repo),
                        "--adopt-run",
                        "legacy-run",
                        "--force",
                        "--json",
                    ]
                )

            self.assertEqual(rejected, 1)
            self.assertIn("--force", stderr.getvalue())
            self.assertEqual(adopted, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["update_status"], "legacy_run_adopted")
            self.assertEqual(
                payload["runtime_release_binding"]["release_id"],
                "release-1",
            )

    def test_pre04_17_unsafe_or_incomplete_direct_children_fail_closed(self):
        cases = ("regular-file", "state-symlink", "binding-without-identity")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                work_root = Path(tmp)
                runs = work_root / "runs"
                runs.mkdir(parents=True)
                child = runs / "unsafe"
                if case == "regular-file":
                    child.write_text("not a run\n", encoding="utf-8")
                elif case == "state-symlink":
                    child.mkdir()
                    external = work_root / "external-state"
                    external.mkdir()
                    (child / "state").symlink_to(external, target_is_directory=True)
                else:
                    (child / "state").mkdir(parents=True)
                    _write_json(
                        child / "state" / "runtime_release_binding.v1.json",
                        {},
                    )

                with self.assertRaises(AgentTeamReleaseError):
                    scan_run_identities(work_root, expected_project_key="pre04")

    def test_pre04_18_launcher_rejects_schema_extras_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            pair = self._publish_pre04_run(work_root, release, "run-1")
            identity_path = Path(pair["run_dir"]) / "state" / "run_identity.v1.json"
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identity["unexpected"] = True
            _write_json(identity_path, identity)
            launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

            with self.assertRaises(launcher["LauncherError"]):
                launcher["_validate_run_pair"](pair["run_dir"], "pre04")

    def test_pre04_19_help_and_supervision_probe_do_not_select_a_run(self):
        launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

        self.assertIsNone(launcher["_launcher_selection"](["gate", "--help"]))
        self.assertIsNone(
            launcher["_launcher_selection"](
                ["doctor", "--invocation-supervision-probe"]
            )
        )

    def test_pre04_20_invalid_paired_identity_variants_fail_closed(self):
        cases = ("duplicate-sequence", "wrong-project", "directory-mismatch", "missing-binding")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                work_root = Path(tmp)
                release = _pre04_release_fixture(work_root, "release-1")
                first = self._publish_pre04_run(work_root, release, "run-1")
                second = self._publish_pre04_run(work_root, release, "run-2")
                identity_path = Path(second["run_dir"]) / "state" / "run_identity.v1.json"
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                if case == "duplicate-sequence":
                    identity["creation_sequence"] = first["identity"]["creation_sequence"]
                    _write_json(identity_path, identity)
                elif case == "wrong-project":
                    identity["project_key"] = "another-project"
                    _write_json(identity_path, identity)
                elif case == "directory-mismatch":
                    identity["run_id"] = "not-run-2"
                    _write_json(identity_path, identity)
                else:
                    (
                        Path(second["run_dir"])
                        / "state"
                        / "runtime_release_binding.v1.json"
                    ).unlink()

                with self.assertRaises(AgentTeamReleaseError):
                    select_latest_implementation_run(
                        work_root,
                        expected_project_key="pre04",
                    )

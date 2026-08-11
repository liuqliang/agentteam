import json
import hashlib
import copy
import os
import re
import shutil
import sqlite3
import stat
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
from agentteam_runtime.decision_runtime import (
    load_run_decision_binding,
    publish_run_decision_binding,
)
from agentteam_runtime.goal_memory import build_goal_memory, render_goal_memory_prompt_context
from agentteam_runtime.notifications import FeishuWebhookNotifier, _permission_request_text
from agentteam_runtime.operator_report import (
    aggregate_model_invocation_usage,
    build_run_completion_report,
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
import agentteam_runtime.taskpack_author as taskpack_author_module
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
    if "contract" in approval["digest_bindings"]:
        record["contract_decisions_sha256"] = (
            taskpack_module._sha256_json(blueprint.get("contract"))
        )
    if "decision_contract" in approval["digest_bindings"]:
        record["decision_contract_sha256"] = (
            taskpack_module._sha256_json(blueprint.get("decision_contract"))
        )
    _write_json(repo / approval["record_path"], record)
    return record


def _blueprint_decision_contract(task_ids):
    def decision(decision_id, kind, parent):
        return {
            "schema_version": "decision_record.v1",
            "decision_id": decision_id,
            "revision": 1,
            "decision_kind": kind,
            "subject": (
                "approved_taskpack_route"
                if kind == "direction"
                else "approved_taskpack_execution"
            ),
            "authority_level": "L2",
            "parent_decision_id": parent,
            "supersedes_decision_id": None,
            "statement": "Execute the approved decision-aware blueprint.",
            "selected_option": "bounded_execution",
            "alternatives": [],
            "rationale": "The blueprint review approved this bounded route.",
            "scope": ["taskpack"],
            "expected_outcome": "Bound tasks complete with declared evidence.",
            "acceptance_refs": ["verification-command"],
            "status": "active",
            "created_at": "2026-08-06T00:00:00Z",
            "created_by": "taskpack-semantic-authority",
            "previous_revision_sha256": None,
        }

    root_id = "DEC-blueprint-direction"
    execution_id = "DEC-blueprint-execution"
    return {
        "schema_version": "taskpack_decision_contract.v1",
        "root_decision_id": root_id,
        "decisions": [
            decision(root_id, "direction", None),
            decision(execution_id, "execution", root_id),
        ],
        "task_bindings": {
            task_id: execution_id for task_id in task_ids
        },
    }


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
                "deterministic_calibration_request": dict(binding),
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



__all__ = [name for name in globals() if not name.startswith('__')]

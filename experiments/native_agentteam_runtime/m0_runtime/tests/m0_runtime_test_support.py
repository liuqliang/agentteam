import errno
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    FileScheduler,
    FileSchedulerDaemon,
    FileMailboxExternalRuntimeAdapter,
    FileMailboxRuntimeAdapter,
    FileMailboxSubprocessRuntimeAdapter,
    FileMailboxWorkerProcessSupervisor,
    FileMailboxWorkerPoolSupervisor,
    FileMailboxWorker,
    ShellRuntimeAdapter,
    TwoPhaseFileScheduler,
    answer_manual_gate,
    audit_worktree_diff,
    build_planner_context,
    build_deterministic_repo_map_handoff,
    build_repo_grounding,
    build_repo_context,
    build_repository_map,
    build_runtime_observability,
    classify_attempt_outcome,
    normalize_task_proposal,
    normalize_evidence_summary,
    read_integration_batches,
    read_integration_queue,
    read_scheduler_state_index,
    replay_events,
    run_file_daemon,
    run_scheduler_loop,
    run_simulation,
    verify_integration_batch,
)
from agentteam_runtime.agentteam import _run_runtime_command_with_progress
from agentteam_runtime.cli import (
    MAX_RETAINED_SUPERVISION_SNAPSHOTS,
    _record_supervision_snapshot,
    _run_supervised_two_phase_scheduler,
)
from agentteam_runtime.experiment_controller import create_experiment_controller
from agentteam_runtime.m0_runtime import (
    apply_patch_to_integration_worktree,
    ensure_integration_baseline_worktree,
    run_integration_verification,
    run_integration_verification_additions,
)
from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    InvocationLifecycle,
    ModelInvocationIntegrityError,
    ModelInvocationWriterRevoked,
    ProviderExecution,
    SystemdGatedExecution,
    assess_execution_group_fence,
    authoritative_provider_snapshot,
    import_model_invocation_lifecycle,
    replay_model_invocation_events,
)
from agentteam_runtime.runtime_artifacts import (
    persist_runtime_artifacts,
    seed_runtime_artifact,
    validate_runtime_input_artifacts,
)
from agentteam_runtime.two_phase_scheduler import _operator_task_report, _runtime_evidence_summary


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"


class FixedClock:
    def __init__(self):
        self._base = datetime(2026, 5, 31, tzinfo=UTC)
        self._seconds = 0

    def now(self):
        value = self._base + timedelta(seconds=self._seconds)
        self._seconds += 1
        return value.isoformat().replace("+00:00", "Z")


def _model_invocation_message():
    return {
        "message_id": "MSG-0001",
        "from_agent": "agent-scheduler",
        "to_agent": "agent-implementation-worker-1",
        "message_type": "dispatch_task",
        "correlation_id": "TASK-001:ATTEMPT-001",
        "payload": _model_invocation_context(
            coverage_class=None,
            include_schema_only_fields=False,
        ),
    }


def _model_invocation_context(
    *,
    coverage_class,
    attempt_id="ATTEMPT-001",
    include_schema_only_fields=True,
    **overrides,
):
    context = {
        "project": "agentteam",
        "run_id": "RUN-001",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "TASKPACK-001",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "TASK-001",
        "attempt_id": attempt_id,
        "runtime_execution_session_id": f"SESSION-{attempt_id}",
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": "LEASE-001",
        "lease_id": "LEASE-001",
        "agent_id": "agent-implementation-worker-1",
        "agent_role": "implementation_worker",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": None,
        "coverage_class": coverage_class,
        "provider_usage_scope": None,
        "provider_session_lock_held": False,
        "provider_project_binding_valid": False,
        "provider_lineage_status": None,
        "previous_provider_session_id": None,
        "previous_provider_turn_id": None,
        "previous_invocation_id": None,
        "objective": "Exercise model invocation lifecycle.",
        "read_scope": ["."],
        "write_scope": [],
    }
    context.update(overrides)
    if not include_schema_only_fields:
        context.pop("role")
        context.pop("backend")
    return context


def _supported_execution_group_identity():
    return ExecutionGroupIdentity(
        gated_supervisor_pid=321,
        gated_supervisor_pgid=321,
        host_boot_id="12345678-1234-1234-1234-123456789abc",
        gated_supervisor_start_ticks=4567,
        launch_nonce_sha256="c" * 64,
        systemd_linger_enabled=True,
        systemd_transient_unit="agentteam-inv-test.service",
        systemd_transient_invocation_id="a" * 32,
        systemd_transient_kill_mode="control-group",
        systemd_user_manager_identity="manager-monotonic:998877",
        systemd_transient_control_group="/user.slice/transient-service",
        systemd_user_service_invocation_id="b" * 32,
        systemd_user_service_control_group="/user.slice/user-service",
        systemd_user_service_kill_mode="mixed",
    )


def _validate_model_invocation_record(schema_name, record):
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(record)


def _init_git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "config", "user.email", "agentteam@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "AgentTeam Test"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_rev_parse(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _git_status_short(repo):
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _planner_message(tmp_path):
    context_path = Path(tmp_path) / "planner_contexts" / "DECOMPOSE-M23-001.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(
            {
                "context_schema_version": "planner_context.v1",
                "milestone_id": "M23",
                "default_worker_role": "repo_map_agent",
                "allowed_read_scopes": ["."],
                "allowed_write_scopes": ["generated/"],
                "available_agent_roles": ["repo_map_agent", "task_planner"],
                "proposal_contract": {
                    "schema_version": "task_proposal.v1",
                    "required_fields": [
                        "task_id",
                        "objective",
                        "read_scope",
                        "write_scope",
                        "required_role",
                        "risk_target",
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "message_id": "MSG-0001",
        "from_agent": "agent-scheduler",
        "to_agent": "agent-planner",
        "message_type": "dispatch_task",
        "correlation_id": "DECOMPOSE-M23-001:ATTEMPT-001",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "DECOMPOSE-M23-001",
            "attempt_id": "DECOMPOSE-M23-001-ATTEMPT-001",
            "lease_id": "DECOMPOSE-M23-001-LEASE-001",
            "task_kind": "decompose_backlog",
            "milestone_id": "M23",
            "default_worker_role": "repo_map_agent",
            "planner_context_path": str(context_path),
            "objective": "Generate bounded backlog tasks.",
            "read_scope": ["."],
            "write_scope": [],
        },
    }


def _read_first_jsonl(path):
    return json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])


def _read_jsonl_for_test(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_test_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _mailbox_dispatch_message(message_id, agent_id, write_scope):
    return {
        "message_id": message_id,
        "from_agent": "agent-scheduler",
        "to_agent": agent_id,
        "message_type": "dispatch_task",
        "correlation_id": f"TASK-MAILBOX:{message_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "TASK-MAILBOX",
            "attempt_id": "ATTEMPT-MAILBOX-001",
            "lease_id": "LEASE-MAILBOX-001",
            "worktree_id": "WT-MAILBOX-001",
            "worktree_path": None,
            "branch": None,
            "objective": "Exercise file mailbox worker runtime.",
            "read_scope": ["."],
            "write_scope": write_scope,
        },
    }


def _write_backlog(tmp_path, write_scope, tasks=None):
    backlog = {
        "backlog_id": "BL-TEST",
        "items": [_backlog_task("TASK-001", write_scope=write_scope)] if tasks is None else tasks,
    }
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    return path


def _write_agent_pool_with_runtime_profile(path, runtime_profile):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-02T00:00:00Z",
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "runtime_profile": runtime_profile,
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool), encoding="utf-8")


def _write_agent_pool_with_role_runtime_profiles(path, role_runtime_profiles):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_runtime_profiles": role_runtime_profiles,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_role_prompt_contracts(path, role_prompt_contracts):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_prompt_contracts": role_prompt_contracts,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_role_context_packages(path, role_context_packages):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_context_packages": role_context_packages,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_id(path, agent_id):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_ids(path, agent_ids):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent" if index == 0 else f"aux_role_{index}",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for index, agent_id in enumerate(agent_ids)
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_roles(path, agent_roles):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": role,
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for agent_id, role in agent_roles
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _append_runtime_result(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-repo-map",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": {
                "test": "m18",
                "operator_summary": {
                    "what_changed": ["模拟 worker 已完成任务。"],
                    "verification_summary": ["simulated_worker: 已通过"],
                    "deliverables": [
                        {
                            "deliverable": "goal_alignment_summary",
                            "summary": "模拟 worker 已保留任务目标对齐。",
                            "evidence": ["test helper"],
                        },
                        {
                            "deliverable": "implemented_changes_or_no_safe_change_rationale",
                            "summary": "模拟 worker 已生成声明的变更文件。",
                            "evidence": changed_files or ["未生成变更文件"],
                        },
                        {
                            "deliverable": "verification_summary",
                            "summary": "模拟验证已通过。",
                            "evidence": ["test helper"],
                        },
                        {
                            "deliverable": "next_steps",
                            "summary": "没有额外的模拟下一步。",
                            "evidence": ["test helper"],
                        },
                    ],
                },
            },
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _append_runtime_result_with_output(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
    output,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-planner",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": output,
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _backlog_task(
    task_id,
    write_scope,
    status="ready",
    depends_on=None,
    blockers=None,
    required_role="repo_map_agent",
):
    return {
        "task_id": task_id,
        "milestone_id": "M0",
        "objective": f"Create generated repo index for {task_id}.",
        "backlog_status": status,
        "risk_target": "L0",
        "depends_on": list(depends_on or []),
        "read_scope": ["."],
        "write_scope": write_scope,
        "required_role": required_role,
        "blockers": list(blockers or []),
    }


def _event_record(event_id, sequence):
    return {
        "actor": "agent-scheduler",
        "correlation_id": "RUN-TEST",
        "event_id": event_id,
        "event_type": "scheduler_started",
        "idempotency_key": f"scheduler-start:{sequence}",
        "payload": {"pool_id": "test"},
        "sequence": sequence,
        "target_agent_id": None,
        "time": f"2026-05-31T00:00:{sequence:02d}Z",
    }


def _write_success_worker(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "message = json.load(sys.stdin)",
                f"target = pathlib.Path({changed_file!r})",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'attempt_id': message['payload']['attempt_id']}), encoding='utf-8')",
                "print(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'shell'}",
                "}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "message = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "required = message['payload'].get('required_deliverables', [])",
                "deliverables = [",
                "    {'deliverable': item, 'summary': f'已满足 {item}', 'evidence': ['fake codex']}",
                "    for item in required",
                "]",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'saw_prompt': 'dispatch_task' in prompt}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'prompt_contains_contract': 'changed_files' in prompt,",
                "        'operator_summary': {",
                "            'what_changed': ['Fake Codex 已写入请求的文件。'],",
                "            'verification_summary': ['fake_codex: 已通过'],",
                "            'deliverables': deliverables,",
                "        },",
                "    },",
                "}), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex_planner(path, dirty_file=None):
    lines = [
        "import json",
        "import pathlib",
        "import sys",
        "args = sys.argv[1:]",
        "prompt = sys.stdin.read()",
        "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
        "worktree = pathlib.Path(args[args.index('-C') + 1])",
        "if 'AgentTeam planner' not in prompt or 'task_proposal' not in prompt:",
        "    sys.exit(7)",
    ]
    if dirty_file:
        lines.extend(
            [
                f"dirty = worktree / {dirty_file!r}",
                "dirty.parent.mkdir(parents=True, exist_ok=True)",
                "dirty.write_text('dirty planner change\\n', encoding='utf-8')",
            ]
        )
    lines.extend(
        [
            "output_path.parent.mkdir(parents=True, exist_ok=True)",
            "output_path.write_text(json.dumps({",
            "    'result_status': 'completed',",
            "    'changed_files': [],",
            "    'output': {",
            "        'adapter': 'codex',",
            "        'task_proposal': {",
            "            'milestone_id': 'M23',",
            "            'tasks': [{",
            "                'task_id': 'TASK-M23-CODEX-001',",
            "                'objective': 'Run generated Codex planner worker task.',",
            "                'read_scope': ['.'],",
            "                'write_scope': ['generated/'],",
            "                'required_role': 'repo_map_agent',",
            "                'risk_target': 'L0',",
            "                'depends_on': [],",
            "                'blockers': [],",
            "            }],",
            "        },",
            "    },",
            "}), encoding='utf-8')",
            "print(json.dumps({'event': 'fake_codex_planner_done'}))",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_fake_codex_planner_and_worker(path):
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
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "if 'AgentTeam planner' in prompt:",
                "    if 'task_proposal' not in prompt:",
                "        sys.exit(7)",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': [],",
                "        'output': {",
                "            'adapter': 'codex',",
                "            'task_proposal': {",
                "                'milestone_id': 'M23',",
                "                'tasks': [{",
                "                    'task_id': 'TASK-M23-CODEX-001',",
                "                    'objective': 'Run generated Codex planner worker task.',",
                "                    'read_scope': ['.'],",
                "                    'write_scope': ['generated/'],",
                "                    'required_role': 'repo_map_agent',",
                "                    'risk_target': 'L0',",
                "                    'depends_on': [],",
                "                    'blockers': [],",
                "                }],",
                "            },",
                "        },",
                "    }), encoding='utf-8')",
                "else:",
                "    message = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "    required = message['payload'].get('required_deliverables', [])",
                "    deliverables = [",
                "        {'deliverable': item, 'summary': f'已满足 {item}', 'evidence': ['fake codex']}",
                "        for item in required",
                "    ]",
                "    target = worktree / 'generated' / 'codex_generated_worker.json'",
                "    target.parent.mkdir(parents=True, exist_ok=True)",
                "    target.write_text(json.dumps({'generated_by': 'fake_codex_worker'}), encoding='utf-8')",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': ['generated/codex_generated_worker.json'],",
                "        'output': {",
                "            'adapter': 'codex',",
                "            'operator_summary': {",
                "                'what_changed': ['Fake Codex worker 已写入生成结果。'],",
                "                'verification_summary': ['fake_codex_worker: 已通过'],",
                "                'deliverables': deliverables,",
                "            },",
                "        },",
                "    }), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_planner_and_worker_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _arg_value(args, flag):
    return args[args.index(flag) + 1]


def _write_fake_codex_arg_recorder(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "sandbox = args[args.index('-s') + 1]",
                "model = args[args.index('-m') + 1] if '-m' in args else None",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({",
                "    'argv': args,",
                "    'model': model,",
                "    'sandbox': sandbox,",
                "}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'mode': 'fake-options'}",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_hanging_mailbox_worker(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import pathlib",
                "import sys",
                "import time",
                "args = sys.argv[1:]",
                "worktree = pathlib.Path(args[args.index('--worktree-path') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.write_text(target.read_text(encoding='utf-8') + '\\nsalvaged timeout change\\n', encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )



__all__ = [name for name in globals() if not name.startswith('__')]

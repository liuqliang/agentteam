import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from agentteam_runtime.experiment_budget import (
    ExperimentBudgetController,
    ExperimentBudgetError,
    ExperimentBudgetIntegrityError,
    advance_experiment_budget,
    create_experiment_budget_state,
    validate_experiment_budget_state,
)
from agentteam_runtime.experiment_calibration import (
    ExperimentCalibrationError,
    load_deterministic_calibration_report,
    run_deterministic_experiment_calibration,
)
from agentteam_runtime.experiment_controller import (
    ExperimentControllerError,
    ExperimentControllerIntegrityError,
    ExperimentProviderAdmissionDenied,
    create_experiment_controller,
)
from agentteam_runtime.experiment_gates import (
    Phase2GateError,
    execute_live_calibration_action,
    execute_phase2_finalization_action,
    execute_readiness_promotion_action,
    publish_live_authorization,
    require_live_provider_authorization,
    resolve_gate_spec,
    run_gate_controller,
    validate_gate_relation,
)
from agentteam_runtime.experiment_readiness import (
    build_p0_readiness_summary,
)
from agentteam_runtime.experiment_ledger import (
    ExperimentLedgerError,
    ExperimentLedgerIntegrityError,
    ExperimentOperatorActionLimitExceeded,
    create_experiment_operator_action_ledger,
    load_experiment_operator_action_ledger,
    validate_experiment_operator_action,
)
from agentteam_runtime.experiment_results import (
    ExperimentResultConflict,
    ExperimentResultIntegrityError,
    build_experiment_result_bundle,
    load_experiment_result_bundle,
    load_latest_experiment_recovery_snapshot,
    measure_experiment_artifacts,
    publish_result_scan_scope,
    render_experiment_comparison,
    render_experiment_result,
    seal_experiment_result_bundle,
    write_experiment_recovery_snapshot,
)
from agentteam_runtime.experiment_modes import (
    _begin_mode_execution,
    _complete_mode_execution,
    _publish_mode_order_authority,
    _register_provider_launch,
    AgentTeamDirectModeAdapter,
    AgentTeamFullModeAdapter,
    ExperimentCommonFinalizer,
    ExperimentModeController,
    ExperimentModeError,
    NativeSingleCodexProvider,
    SingleCodexModeAdapter,
    execute_bound_experiment_mode,
)
from agentteam_runtime.experiment_results import _publish_sealed_directory
from agentteam_runtime.experiment_contract import (
    LEGACY_MANIFEST_SCHEMA_VERSION,
    ExperimentContractError,
    ExperimentLeaseError,
    acquire_controller_lease,
    allocate_experiment_run,
    build_experiment_run_manifest,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_experiment_run_id,
    ensure_executable_manifest,
    publish_immutable_json,
    validate_experiment_protocol,
    validate_experiment_run_binding,
    validate_experiment_run_manifest,
    validate_experiment_state,
    validate_resume_binding,
)
from agentteam_runtime.experiment_workspace import (
    ExperimentWorkspaceError,
    allocate_clean_snapshot,
    cleanup_clean_snapshot,
    load_clean_snapshot_attestation,
    validate_clean_snapshot_attestation,
    verify_clean_snapshot,
)
from agentteam_runtime.token_usage import usage_event_id_for_invocation
from agentteam_runtime.experiment_sandbox import (
    _capture_bounded_process,
    _candidate_repository_state,
    _approved_acceptance_executable,
    _read_digest_bound_evaluator,
    _run_bounded_argv,
    _sandbox_policy_sha256,
    ExperimentEvaluationBlocked,
    ExperimentSandboxError,
    ExperimentSandboxUnavailable,
    _attach_namespace_evidence,
    build_provider_sandbox_descriptor,
    experiment_lifecycle_authority_root,
    prepare_candidate_evaluation_launch,
    prepare_provider_launch,
    probe_gold_canary_denial,
    publish_evaluator_reference,
    publish_experiment_launch_registration,
    publish_experiment_mode_authority,
    publish_experiment_protocol_reference,
    publish_model_invocation_set_reference,
    publish_registered_model_invocation_set_reference,
    publish_provider_sandbox_reference,
    publish_scan_scope_reference,
    run_trusted_argv_evaluator,
    scan_canary_leakage,
    scan_scope_sha256,
    validate_evaluation_evidence,
    validate_provider_sandbox_descriptor,
)
from agentteam_runtime.model_invocation import (
    _validate_registered_codex_command,
    ExecutionGroupIdentity,
    InvocationLifecycle,
    ModelInvocationCall,
    ModelInvocationIntegrityError,
    ModelInvocationUnavailable,
    ProviderExecution,
    invocation_context_from_message,
)
from agentteam_runtime.m0_runtime import (
    _with_codex_reasoning_profile,
    create_independent_attempt_workspace,
)
from agentteam_runtime.taskpack import (
    draft_taskpack_files,
    freeze_taskpack,
)
from agentteam_runtime.taskpack_author import (
    _registered_experiment_author_output,
)
from agentteam_runtime.mailbox_worker import (
    FileMailboxWorker,
    _model_invocation_context_payload,
)
import agentteam_runtime.cli as cli_module
import agentteam_runtime.experiment_gates as experiment_gates_module
import agentteam_runtime.experiment_ledger as experiment_ledger_module
from agentteam_runtime.operator_control import (
    answer_experiment_manual_gate,
    cleanup_stale_runs,
    record_experiment_operator_event,
    resume_experiment_run,
    resolve_experiment_permission_request,
    stop_run,
    stop_experiment_run,
)
from agentteam_runtime.projection_db import (
    check_project_projection_db,
    read_projected_experiment_recovery,
    read_projected_experiment_results,
    rebuild_project_projection_db,
)
import agentteam_runtime.two_phase_scheduler as two_phase_scheduler_module
from agentteam_runtime.two_phase_scheduler import (
    TwoPhaseFileScheduler,
    reconcile_orphaned_invocation,
)


def _protocol():
    return {
        "schema_version": "experiment_protocol.v1",
        "experiment_id": "phase2-calibration",
        "instance_id": "fixture-001",
        "repository": {
            "source": "/srv/source/repository.git",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "git_object_format": "sha1",
        },
        "goal": {
            "summary": "Implement the deterministic fixture.",
            "constraints": [
                "Do not read evaluator-only state.",
                "Preserve the declared acceptance command.",
            ],
        },
        "acceptance": {
            "command": [
                str(Path(sys.executable).resolve()),
                "-m",
                "unittest",
                "tests.test_fixture",
            ],
            "timeout_seconds": 120,
        },
        "modes": [
            "single_codex",
            "agentteam_direct",
            "agentteam_full",
        ],
        "mode_order": [
            "agentteam_direct",
            "agentteam_full",
            "single_codex",
        ],
        "mode_instructions": {
            "single_codex": [],
            "agentteam_direct": ["Use only the preregistered taskpack."],
            "agentteam_full": ["Author and freeze the taskpack normally."],
        },
        "repetition_policy": {
            "count": 2,
            "order_strategy": "seeded_counterbalanced_rotation",
        },
        "environment": {
            "backend": "codex",
            "codex_cli_version": "codex-test-v1",
            "model": "codex-test-model",
            "reasoning_profile": "high",
            "service_configuration_sha256": "3" * 64,
            "sandbox_policy": "workspace-write",
            "permission_policy": "never",
            "network_policy": "disabled",
            "tool_allowlist": ["exec_command", "apply_patch"],
            "host_class": "deterministic-test",
            "cpu_limit": 2,
            "memory_limit_bytes": 1073741824,
            "dependency_cache_policy": "read_only_preregistered",
            "max_inflight_model_invocations": 1,
        },
        "seed": 17,
        "scored": False,
        "blind_gold": {
            "policy": "unavailable_to_runtime",
        },
        "budgets": {
            "max_total_tokens": 100000,
            "max_wall_time_seconds": 1800,
            "soft_warning_ratio": 0.8,
            "stop_boundaries": [
                "pre_provider_launch",
                "post_invocation_terminal",
                "pre_integration",
                "post_integration",
            ],
        },
        "operator_limits": {
            "expected_operator_action": 2,
            "corrective_intervention": 1,
            "decision_escalation": 1,
        },
        "evaluator": {
            "version": "fixture-evaluator.v1",
            "artifact_sha256": "4" * 64,
        },
        "direct_taskpack": {
            "sha256": "5" * 64,
            "cost_reported_separately": True,
            "available_to_full_mode": False,
        },
        "usage_contract_version": "model_invocation_usage.v1",
    }


def _release():
    return {
        "release_id": "candidate-v1",
        "release_root": "/srv/agentteam/releases/candidate-v1",
        "runtime_root": "/srv/agentteam/releases/candidate-v1/m0_runtime",
        "release_manifest_sha256": "6" * 64,
        "source_commit": "7" * 40,
        "git_object_format": "sha1",
    }


def _filesystem_release(
    release_root,
    source_commit,
    *,
    release_id="candidate-v1",
):
    release_root = Path(release_root).resolve()
    runtime_root = (
        release_root
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    package_root = runtime_root / "agentteam_runtime"
    package_root.mkdir(parents=True)
    (package_root / "agentteam.py").write_text(
        "# fixture runtime entrypoint\n",
        encoding="utf-8",
    )
    manifest = {
        "manifest_schema_version": "agentteam_release_manifest.v2",
        "release_id": release_id,
        "release_root": str(release_root),
        "runtime_root": str(runtime_root),
        "source_commit": source_commit,
        "git_object_format": (
            "sha1" if len(source_commit) == 40 else "sha256"
        ),
    }
    manifest_path = release_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "release_id": release_id,
        "release_root": str(release_root),
        "runtime_root": str(runtime_root),
        "release_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "source_commit": source_commit,
        "git_object_format": manifest["git_object_format"],
    }


def _allocate(root, key="request-001"):
    return allocate_experiment_run(
        root,
        _protocol(),
        mode="single_codex",
        repetition_index=0,
        stable_request_key=key,
        runtime_release=_release(),
        bound_at="2026-07-27T00:00:00Z",
    )


def _git(repository, *arguments, check=True):
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed


def _fixture_repository(
    root,
    *,
    escaping_symlink=False,
    object_format="sha1",
):
    source = Path(root) / "source"
    source.mkdir()
    subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            f"--object-format={object_format}",
            str(source),
        ],
        check=True,
    )
    _git(source, "config", "user.name", "Experiment Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    (source / "history.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", "history.txt")
    _git(source, "commit", "--quiet", "-m", "first")
    parent_commit = _git(source, "rev-parse", "HEAD").stdout.strip()

    (source / "history.txt").write_text("second\n", encoding="utf-8")
    (source / "tracked.txt").write_text("clean fixture\n", encoding="utf-8")
    if escaping_symlink:
        os.symlink("../outside-secret", source / "escape")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "protocol source")
    commit = _git(source, "rev-parse", "HEAD").stdout.strip()
    tree = _git(source, "rev-parse", "HEAD^{tree}").stdout.strip()
    object_format = _git(
        source,
        "rev-parse",
        "--show-object-format",
    ).stdout.strip()

    _git(source, "branch", "source-only-branch", parent_commit)
    _git(source, "tag", "source-only-tag", parent_commit)
    _git(source, "remote", "add", "source-only-remote", str(source))
    prior_state = source / ".agentteam"
    prior_state.mkdir()
    (prior_state / "prior-run.json").write_text("{}\n", encoding="utf-8")
    (source / "untracked.patch").write_text("prior patch\n", encoding="utf-8")
    return {
        "repository": {
            "source": str(source),
            "commit": commit,
            "tree": tree,
            "git_object_format": object_format,
        },
        "parent_commit": parent_commit,
        "source": source,
    }


def _sandbox_fixture(root):
    root = Path(root)
    repository = root / "sandbox-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    _git(repository, "config", "user.name", "Sandbox Fixture")
    _git(repository, "config", "user.email", "sandbox@example.invalid")
    (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "sandbox fixture")
    repository_identity = {
        "commit": _git(repository, "rev-parse", "HEAD").stdout.strip(),
        "tree": _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip(),
        "git_object_format": _git(
            repository,
            "rev-parse",
            "--show-object-format",
        ).stdout.strip(),
    }
    credential = root / "provider-credential.json"
    credential.write_text('{"token":"bounded-test-token"}\n', encoding="utf-8")
    canary = root / "evaluator-only" / "gold-canary"
    canary.parent.mkdir()
    canary.write_bytes(b"agentteam-evaluator-only-canary")
    bwrap = Path("/usr/bin/bwrap")
    runtime_views = [
        {"source": path, "target": path}
        for path in ("/usr", "/lib", "/lib64", "/bin")
        if Path(path).exists()
    ]
    descriptor = build_provider_sandbox_descriptor(
        repository,
        runtime_views=runtime_views,
        credential_mounts=[
            {
                "source": str(credential),
                "target": "/run/agentteam-credentials/provider.json",
            }
        ],
        environment={
            "AGENTTEAM_CREDENTIAL_FILE": (
                "/run/agentteam-credentials/provider.json"
            )
        },
        bwrap_path=bwrap,
        repository_identity=repository_identity,
        forbidden_paths=[canary],
    )
    evidence = {
        "schema_version": "experiment_namespace_probe.v1",
        "evidence_status": "complete",
        "denial_status": "denied",
        "policy_sha256": descriptor["policy_sha256"],
        "canary_sha256": hashlib.sha256(canary.read_bytes()).hexdigest(),
        "path_visible": False,
        "content_readable": False,
        "probe_returncode": 0,
    }
    return {
        "repository": repository,
        "repository_identity": repository_identity,
        "credential": credential,
        "canary": canary,
        "evidence": evidence,
        "descriptor": _attach_namespace_evidence(descriptor, evidence),
        "uncertified_descriptor": descriptor,
    }


def _publish_test_sandbox_reference(authority_root, fixture):
    with patch(
        "agentteam_runtime.experiment_sandbox.probe_gold_canary_denial",
        return_value=fixture["evidence"],
    ):
        return publish_provider_sandbox_reference(
            authority_root,
            fixture["uncertified_descriptor"],
            fixture["canary"],
        )


def _successful_namespace_probe(descriptor, canary_path, **_kwargs):
    return {
        "schema_version": "experiment_namespace_probe.v1",
        "evidence_status": "complete",
        "denial_status": "denied",
        "policy_sha256": descriptor["policy_sha256"],
        "canary_sha256": hashlib.sha256(
            Path(canary_path).read_bytes()
        ).hexdigest(),
        "path_visible": False,
        "content_readable": False,
        "probe_returncode": 0,
    }


def _supported_execution_identity():
    return ExecutionGroupIdentity(
        gated_supervisor_pid=os.getpid(),
        gated_supervisor_pgid=os.getpgrp(),
        host_boot_id="00000000-0000-0000-0000-000000000001",
        gated_supervisor_start_ticks=1,
        launch_nonce_sha256="f" * 64,
        systemd_linger_enabled=True,
        systemd_transient_unit="agentteam-fixture.service",
        systemd_transient_invocation_id="1" * 32,
        systemd_transient_kill_mode="control-group",
        systemd_user_manager_identity="fixture-user-manager",
        systemd_transient_control_group=(
            "/user.slice/agentteam-fixture.service"
        ),
        systemd_user_service_invocation_id="2" * 32,
        systemd_user_service_control_group=(
            "/user.slice/agentteam-fixture.service"
        ),
        systemd_user_service_kill_mode="control-group",
    )


def _complete_fake_experiment_invocation(
    invocation_context,
    workspace_root,
    *,
    role,
    execution_identity=None,
):
    class FakeGatedRunner:
        def __init__(
            self,
            lifecycle,
            command,
            *,
            cwd,
            input_text,
            timeout_seconds,
            environment=None,
        ):
            del lifecycle, cwd, input_text, timeout_seconds, environment
            self.command = list(command)

        def prepare(self):
            return (
                execution_identity
                or ExecutionGroupIdentity.not_applicable()
            )

        def permit_and_wait(self, **_kwargs):
            return ProviderExecution(
                self.command,
                0,
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 1,
                            "cached_input_tokens": 0,
                            "output_tokens": 1,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 2,
                        },
                    }
                ),
                "",
            )

        def abort_before_permit(self):
            return None

        def cleanup_after_terminal(self):
            return None

    context = _model_context(
        supported=True,
        sandbox_reference=None,
    )
    context.pop("experiment_sandbox_reference", None)
    context.update(invocation_context)
    context.update(
        {
            "project": Path(workspace_root).name,
            "runtime_execution_session_id": (
                context.get("runtime_execution_session_id")
                or f"SESSION-{uuid.uuid4().hex}"
            ),
            "lifecycle_owner_token": (
                context.get("lifecycle_owner_token")
                or f"OWNER-{uuid.uuid4().hex}"
            ),
            "agent_id": (
                context.get("agent_id") or f"{role}-fixture"
            ),
            "role": role,
            "backend": "codex",
            "coverage_class": "supported_model_invocation",
        }
    )
    invocation = ModelInvocationCall(
        context["model_invocation_authority_root"],
        context,
        supported=True,
        systemd_runner_factory=FakeGatedRunner,
    )
    execution = invocation.execute(
        [
            "codex",
            "exec",
            "-m",
            context["model"],
            "-c",
            (
                "model_reasoning_effort="
                f"{context['reasoning_profile']}"
            ),
        ],
        cwd=workspace_root,
        input_text="deterministic experiment fixture",
        timeout_seconds=10,
    )
    invocation.finalize("completed", execution)
    return invocation.lifecycle.invocation_id


def _complete_fake_runtime_invocation(
    experiment_context,
    workspace_root,
    *,
    lifecycle_id,
    taskpack_id,
    usage_stage="implementation_worker",
    execution_identity=None,
):
    configuration = experiment_context["sandbox_configuration"]
    lifecycle_root = experiment_lifecycle_authority_root(
        experiment_context["authority_root"],
        lifecycle_id,
    )
    descriptor = build_provider_sandbox_descriptor(
        workspace_root,
        runtime_views=configuration["runtime_views"],
        library_views=configuration["library_views"],
        credential_mounts=configuration["credential_mounts"],
        environment=configuration["environment"],
        repository_identity={
            field: experiment_context["repository_identity"][field]
            for field in ("commit", "tree", "git_object_format")
        },
        forbidden_paths=[configuration["canary_path"]],
    )
    sandbox_reference = publish_provider_sandbox_reference(
        experiment_context["authority_root"],
        descriptor,
        configuration["canary_path"],
        reference_id=f"{lifecycle_id}-sandbox",
    )
    publish_experiment_launch_registration(
        experiment_context["authority_root"],
        lifecycle_root,
        experiment_run_id=experiment_context["experiment_run_id"],
        protocol_sha256=experiment_context["protocol_sha256"],
        run_manifest_sha256=experiment_context[
            "run_manifest_sha256"
        ],
        mode=experiment_context["mode"],
        usage_stage=usage_stage,
        taskpack_id=taskpack_id,
        workspace_root=workspace_root,
        sandbox_reference=sandbox_reference,
        controller_reference=experiment_context[
            "controller_reference"
        ],
        model_policy=experiment_context["model_policy"],
    )
    return _complete_fake_experiment_invocation(
        {
            "model_invocation_authority_root": str(lifecycle_root),
            "run_id": experiment_context["experiment_run_id"],
            "taskpack_id": taskpack_id,
            "usage_stage": usage_stage,
            "model": experiment_context["model_policy"]["model"],
            "reasoning_profile": experiment_context["model_policy"][
                "reasoning_profile"
            ],
            "provider_resume_mode": "new",
        },
        workspace_root,
        role="implementation_worker",
        execution_identity=execution_identity,
    )


def _start_orphaned_fake_runtime_invocation(
    experiment_context,
    workspace_root,
    *,
    lifecycle_id,
    taskpack_id,
):
    configuration = experiment_context["sandbox_configuration"]
    lifecycle_root = experiment_lifecycle_authority_root(
        experiment_context["authority_root"],
        lifecycle_id,
    )
    descriptor = build_provider_sandbox_descriptor(
        workspace_root,
        runtime_views=configuration["runtime_views"],
        library_views=configuration["library_views"],
        credential_mounts=configuration["credential_mounts"],
        environment=configuration["environment"],
        repository_identity={
            field: experiment_context["repository_identity"][field]
            for field in ("commit", "tree", "git_object_format")
        },
        forbidden_paths=[configuration["canary_path"]],
    )
    sandbox_reference = publish_provider_sandbox_reference(
        experiment_context["authority_root"],
        descriptor,
        configuration["canary_path"],
        reference_id=f"{lifecycle_id}-sandbox",
    )
    publish_experiment_launch_registration(
        experiment_context["authority_root"],
        lifecycle_root,
        experiment_run_id=experiment_context["experiment_run_id"],
        protocol_sha256=experiment_context["protocol_sha256"],
        run_manifest_sha256=experiment_context[
            "run_manifest_sha256"
        ],
        mode=experiment_context["mode"],
        usage_stage="implementation_worker",
        taskpack_id=taskpack_id,
        workspace_root=workspace_root,
        sandbox_reference=sandbox_reference,
        controller_reference=experiment_context[
            "controller_reference"
        ],
        model_policy=experiment_context["model_policy"],
    )

    class InterruptedRunner:
        def __init__(
            self,
            lifecycle,
            command,
            *,
            cwd,
            input_text,
            timeout_seconds,
            environment=None,
        ):
            del lifecycle, cwd, input_text, timeout_seconds, environment
            self.command = list(command)

        def prepare(self):
            return ExecutionGroupIdentity.not_applicable()

        def permit_and_wait(self, **_kwargs):
            raise OSError("provider interrupted after durable start")

        def abort_before_permit(self):
            return None

        def cleanup_after_terminal(self):
            return None

    context = _model_context(
        supported=True,
        sandbox_reference=None,
    )
    context.pop("experiment_sandbox_reference", None)
    context.update(
        {
            "model_invocation_authority_root": str(lifecycle_root),
            "run_id": experiment_context["experiment_run_id"],
            "taskpack_id": taskpack_id,
            "usage_stage": "implementation_worker",
            "model": experiment_context["model_policy"]["model"],
            "reasoning_profile": experiment_context["model_policy"][
                "reasoning_profile"
            ],
            "provider_resume_mode": "new",
            "project": Path(workspace_root).name,
            "runtime_execution_session_id": (
                f"SESSION-{uuid.uuid4().hex}"
            ),
            "lifecycle_owner_token": f"OWNER-{uuid.uuid4().hex}",
            "agent_id": "orphaned-worker-fixture",
            "role": "implementation_worker",
            "backend": "codex",
            "coverage_class": "supported_model_invocation",
        }
    )
    invocation = ModelInvocationCall(
        lifecycle_root,
        context,
        supported=True,
        systemd_runner_factory=InterruptedRunner,
    )
    try:
        invocation.execute(
            [
                "codex",
                "exec",
                "-m",
                context["model"],
                "-c",
                (
                    "model_reasoning_effort="
                    f"{context['reasoning_profile']}"
                ),
            ],
            cwd=workspace_root,
            input_text="interrupted fixture",
            timeout_seconds=10,
        )
    except OSError:
        import gc

        invocation = None
        gc.collect()
        return


def _sandbox_protocol(fixture):
    protocol = _protocol()
    protocol["repository"] = {
        "source": str(fixture["repository"]),
        **fixture["repository_identity"],
    }
    return protocol


def _model_context(*, supported, sandbox_reference):
    context = {
        "project": "experiment-fixture",
        "run_id": "RUN-EXPERIMENT-FIXTURE",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase2-fixture",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "P2-02B",
        "attempt_id": "ATTEMPT-P2-02B",
        "runtime_execution_session_id": "SESSION-P2-02B",
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": "OWNER-P2-02B",
        "agent_id": "agent-fixture",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": "fixture-model",
        "coverage_class": (
            "supported_model_invocation"
            if supported
            else "not_applicable_adapter"
        ),
        "experiment_sandbox_reference": sandbox_reference,
    }
    context["_explicit_context_fields"] = {
        field: True
        for field in (
            "project",
            "run_id",
            "taskpack_id",
            "runtime_execution_session_id",
            "lifecycle_owner_token",
            "agent_id",
            "role",
            "usage_stage",
        )
    }
    return context


def _test_evaluator_execution(
    argv,
    *,
    cwd,
    environment,
    timeout_seconds,
    max_output_bytes,
    cpu_limit,
    memory_limit_bytes,
    input_bytes=None,
):
    del cpu_limit, memory_limit_bytes
    result = _capture_bounded_process(
        argv,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        input_bytes=input_bytes,
    )
    result.update(
        {
            "execution_boundary": "systemd_user_transient_service",
            "systemd_unit": "agentteam-eval-" + "a" * 24 + ".service",
        }
    )
    return result


def _budget_usage(
    suffix,
    *,
    input_tokens,
    output_tokens,
    cached_input_tokens=0,
    reasoning_tokens=0,
    usage_status="reported",
    unavailable_reason=None,
):
    invocation_id = f"INV-{suffix}"
    start_sha256 = hashlib.sha256(
        _authority_record_bytes({"invocation_id": invocation_id})
    ).hexdigest()
    return {
        "usage_schema_version": "model_invocation_usage.v1",
        "usage_event_id": usage_event_id_for_invocation(invocation_id),
        "invocation_id": invocation_id,
        "start_sha256": start_sha256,
        "project": "agentteam",
        "run_id": "phase2-budget-fixture",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase2-budget-fixture",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "P2-03A",
        "attempt_id": f"ATTEMPT-{suffix}",
        "runtime_execution_session_id": f"SESSION-{suffix}",
        "provider_session_id": None,
        "provider_predecessor_invocation_id": None,
        "provider_turn_id": None,
        "provider_predecessor_turn_id": None,
        "lifecycle_owner_token": f"LEASE-{suffix}",
        "terminal_writer": "worker",
        "agent_id": "agent-implementation-worker-1",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": None,
        "coverage_class": "supported_model_invocation",
        "terminal_status": "completed",
        "usage_status": usage_status,
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": (
            "provider_reported"
            if usage_status == "reported"
            else (
                "not_applicable"
                if usage_status == "not_applicable"
                else "unavailable"
            )
        ),
        "provider_usage_snapshot": None,
        "unavailable_reason": unavailable_reason,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (
            input_tokens + output_tokens
            if isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            else None
        ),
        "started_at": "2026-07-27T00:00:00Z",
        "finished_at": "2026-07-27T00:00:01Z",
        "wall_time_seconds": 1.0,
        "source_artifact_path": (
            f"model_invocations/{invocation_id}/terminal.json"
        ),
    }


_BUDGET_CLOCK_ONLY = object()


def _authority_record_bytes(record, *, pretty=False):
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _publish_budget_authority(
    state,
    usage,
    *,
    pretty_terminal=False,
    terminal_digest_override=None,
):
    root = Path(state["authority_root"])
    invocation_dir = root / "model_invocations" / usage["invocation_id"]
    invocation_dir.mkdir(parents=True, exist_ok=True)
    start_record = {"invocation_id": usage["invocation_id"]}
    start_bytes = _authority_record_bytes(start_record)
    terminal_bytes = _authority_record_bytes(
        usage,
        pretty=pretty_terminal,
    )
    start_digest = hashlib.sha256(start_bytes).hexdigest()
    terminal_digest = hashlib.sha256(terminal_bytes).hexdigest()
    started_path = invocation_dir / f"started-{start_digest}.json"
    terminal_path = invocation_dir / f"terminal-{terminal_digest}.json"
    if not started_path.exists():
        started_path.write_bytes(start_bytes)
    if not terminal_path.exists():
        terminal_path.write_bytes(terminal_bytes)
    events = [
            {
                "event_id": f"START-{usage['invocation_id']}",
                "event_type": "model_invocation_started",
                "source_event_id": usage["invocation_id"],
                "payload": {
                    **start_record,
                    "_source_artifact_path": (
                        started_path.relative_to(root).as_posix()
                    ),
                    "_source_record_sha256": start_digest,
                },
            },
            {
                "event_id": usage["usage_event_id"],
                "event_type": "model_invocation_usage_recorded",
                "source_event_id": usage["usage_event_id"],
                "payload": {
                    **copy.deepcopy(usage),
                    "_source_artifact_path": (
                        terminal_path.relative_to(root).as_posix()
                    ),
                    "_source_record_sha256": (
                        terminal_digest_override
                        or terminal_digest
                    ),
                },
            },
    ]
    events_path = root / state["authority_events_path"]
    existing_events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with events_path.open("a", encoding="utf-8") as stream:
        for event in events:
            if event in existing_events:
                continue
            stream.write(json.dumps(event, sort_keys=True) + "\n")


def _advance_budget(
    state,
    terminal_usage=_BUDGET_CLOCK_ONLY,
    **kwargs,
):
    if terminal_usage is _BUDGET_CLOCK_ONLY:
        return advance_experiment_budget(state, **kwargs)
    if terminal_usage is None:
        return advance_experiment_budget(state, None, **kwargs)
    _publish_budget_authority(state, terminal_usage)
    return advance_experiment_budget(
        state,
        terminal_usage["usage_event_id"],
        **kwargs,
    )


class ExperimentBudgetTests(unittest.TestCase):
    def _authority_paths(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        events_path = root / "events.jsonl"
        events_path.touch()
        return root, events_path

    def _state(
        self,
        *,
        max_total_tokens=100,
        max_wall_time_seconds=100,
        soft_warning_ratio=0.8,
        initial_monotonic=10,
    ):
        authority_root, authority_events_path = self._authority_paths()
        return create_experiment_budget_state(
            "protocol-global-fixture",
            max_total_tokens,
            max_wall_time_seconds,
            soft_warning_ratio,
            authority_root=authority_root,
            authority_events_path=authority_events_path,
            initial_monotonic=initial_monotonic,
        )

    def _event_validator(self):
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "experiment_budget_event.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_components_stay_separate_and_events_match_declared_schema(self):
        state = self._state(max_total_tokens=200, soft_warning_ratio=0.5)
        usage = _budget_usage(
            "components",
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=30,
            reasoning_tokens=20,
        )

        state, events = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )

        self.assertEqual(
            {
                field: state[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                    "overshoot_tokens",
                )
            },
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 30,
                "reasoning_tokens": 20,
                "total_tokens": 130,
                "overshoot_tokens": 0,
            },
        )
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        validator = self._event_validator()
        validator.validate(events[0])

        invalid = copy.deepcopy(events[0])
        invalid["max_total_tokens"] = True
        self.assertTrue(list(validator.iter_errors(invalid)))
        invalid = copy.deepcopy(events[0])
        invalid["elapsed_wall_time_seconds"] = float("inf")
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_token_warning_and_exhaustion_include_exact_boundaries(self):
        state = self._state()
        state, events = _advance_budget(
            state,
            _budget_usage("below-warning", input_tokens=79, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(events, [])

        state, events = _advance_budget(
            state,
            _budget_usage("at-warning", input_tokens=1, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        self.assertEqual(events[0]["threshold_dimensions"], ["tokens"])

        state, events = _advance_budget(
            state,
            _budget_usage("at-limit", input_tokens=20, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["exhaustion"],
        )
        self.assertTrue(state["exhausted"])
        self.assertEqual(state["overshoot_tokens"], 0)

        state, events = _advance_budget(
            state,
            _budget_usage("above-limit", input_tokens=1, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(events, [])
        self.assertEqual(state["overshoot_tokens"], 1)

    def test_fake_monotonic_clock_drives_exact_wall_boundaries(self):
        ticks = iter([10, 17.999, 18, 30])
        controller = ExperimentBudgetController(monotonic=lambda: next(ticks))
        authority_root, authority_events_path = self._authority_paths()
        state = controller.create_state(
            "clock-fixture",
            100,
            20,
            0.4,
            authority_root=authority_root,
            authority_events_path=authority_events_path,
        )

        state, events = controller.advance(state)
        self.assertEqual(events, [])
        state, events = controller.advance(state)
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        self.assertEqual(state["elapsed_wall_time_seconds"], 8.0)
        state, events = controller.advance(state)
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["exhaustion"],
        )
        self.assertEqual(events[0]["threshold_dimensions"], ["wall_time"])

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "regressed",
        ):
            _advance_budget(state, now_monotonic=29)

    def test_simultaneous_threshold_events_emit_exactly_once(self):
        state = self._state(
            max_total_tokens=100,
            max_wall_time_seconds=10,
            soft_warning_ratio=0.5,
            initial_monotonic=0,
        )
        usage = _budget_usage("simultaneous", input_tokens=100, output_tokens=0)
        state, events = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["warning", "exhaustion"],
        )
        self.assertTrue(
            all(
                event["threshold_dimensions"] == ["tokens", "wall_time"]
                for event in events
            )
        )
        frozen_projection = json.dumps(state, sort_keys=True)

        replayed, replay_events = _advance_budget(
            state,
            copy.deepcopy(usage),
            now_monotonic=10,
        )
        self.assertEqual(replay_events, [])
        self.assertEqual(json.dumps(replayed, sort_keys=True), frozen_projection)
        self.assertEqual(
            [event["event_kind"] for event in replayed["events"]],
            ["warning", "exhaustion"],
        )

    def test_one_lane_terminal_completion_exposes_overshoot(self):
        state = self._state()
        state, _ = _advance_budget(
            state,
            _budget_usage("before-limit", input_tokens=70, output_tokens=20),
            now_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            _budget_usage(
                "inflight-completion",
                input_tokens=20,
                cached_input_tokens=5,
                output_tokens=5,
                reasoning_tokens=2,
            ),
            now_monotonic=11,
        )

        self.assertEqual(state["total_tokens"], 115)
        self.assertEqual(state["input_tokens"], 90)
        self.assertEqual(state["output_tokens"], 25)
        self.assertEqual(state["cached_input_tokens"], 5)
        self.assertEqual(state["reasoning_tokens"], 2)
        self.assertEqual(state["overshoot_tokens"], 15)
        self.assertEqual(events[-1]["event_kind"], "exhaustion")
        self.assertEqual(events[-1]["overshoot_tokens"], 15)

    def test_unavailable_partial_not_applicable_and_missing_fail_closed(self):
        cases = {
            "partial": _budget_usage(
                "partial",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="partial",
                unavailable_reason="incomplete_provider_usage",
            ),
            "unavailable": _budget_usage(
                "unavailable",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="unavailable",
                unavailable_reason="missing_provider_terminal_usage",
            ),
            "not-applicable": _budget_usage(
                "not-applicable",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="not_applicable",
            ),
            "missing-total": _budget_usage(
                "missing-total",
                input_tokens=1,
                output_tokens=1,
            ),
            "missing-terminal": None,
        }
        cases["missing-total"]["total_tokens"] = None

        for name, usage in cases.items():
            with self.subTest(name=name):
                state, _ = _advance_budget(
                    self._state(),
                    usage,
                    now_monotonic=10,
                )
                self.assertEqual(state["total_tokens"], 0)
                self.assertFalse(state["usage_complete"])
                self.assertFalse(state["calibration_eligible"])
                self.assertTrue(state["incomplete_usage_reasons"])

        state = self._state(
            max_wall_time_seconds=1,
            initial_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            cases["unavailable"],
            now_monotonic=11,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["warning", "exhaustion"],
        )
        validator = self._event_validator()
        for event in events:
            validator.validate(event)
            self.assertFalse(event["usage_complete"])
            self.assertEqual(
                event["incomplete_usage_reasons"][0]["usage_status"],
                "unavailable",
            )

    def test_replay_is_idempotent_and_conflicting_identity_fails_closed(self):
        state = self._state()
        usage = _budget_usage("replay", input_tokens=70, output_tokens=10)
        state, _ = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )
        replayed, events = _advance_budget(
            state,
            copy.deepcopy(usage),
            now_monotonic=10,
        )
        self.assertEqual(replayed, state)
        self.assertEqual(events, [])

        rebuilt_state = create_experiment_budget_state(
            state["budget_id"],
            state["max_total_tokens"],
            state["max_wall_time_seconds"],
            state["soft_warning_ratio"],
            authority_root=state["authority_root"],
            authority_events_path=state["authority_events_path"],
            initial_monotonic=state["initial_monotonic"],
        )
        rebuilt, rebuilt_events = advance_experiment_budget(
            rebuilt_state,
            usage["usage_event_id"],
            now_monotonic=10,
        )
        self.assertEqual(rebuilt, state)
        self.assertEqual(
            [event["event_id"] for event in rebuilt_events],
            [event["event_id"] for event in state["events"]],
        )

        conflict = copy.deepcopy(usage)
        conflict["input_tokens"] = 71
        conflict["total_tokens"] = 81
        conflict_state = self._state()
        conflict_state, _ = _advance_budget(
            conflict_state,
            usage,
            now_monotonic=10,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            _advance_budget(
                conflict_state,
                conflict,
                now_monotonic=10,
            )

        duplicate_invocation = copy.deepcopy(usage)
        duplicate_invocation["usage_event_id"] = usage_event_id_for_invocation(
            "INV-replay-second"
        )
        duplicate_state = self._state()
        duplicate_state, _ = _advance_budget(
            duplicate_state,
            usage,
            now_monotonic=10,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            _advance_budget(
                duplicate_state,
                duplicate_invocation,
                now_monotonic=10,
            )

    def test_serialized_projection_cannot_reset_consumption_before_replay(self):
        usage = _budget_usage(
            "projection-reset",
            input_tokens=10,
            output_tokens=2,
        )
        state, _ = _advance_budget(
            self._state(),
            usage,
            now_monotonic=10,
        )
        tampered = copy.deepcopy(state)
        tampered["input_tokens"] = 0
        tampered["output_tokens"] = 0
        tampered["total_tokens"] = 0

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "projection integrity",
        ):
            validate_experiment_budget_state(tampered)
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "projection integrity",
        ):
            _advance_budget(
                tampered,
                copy.deepcopy(usage),
                now_monotonic=10,
            )

    def test_only_canonical_phase1_terminal_usage_can_add_consumption(self):
        canonical = _budget_usage(
            "canonical-authority",
            input_tokens=10,
            output_tokens=2,
        )
        rejected, _ = advance_experiment_budget(
            self._state(),
            canonical,
            now_monotonic=10,
        )
        self.assertEqual(rejected["total_tokens"], 0)
        self.assertFalse(rejected["calibration_eligible"])
        self.assertEqual(
            rejected["incomplete_usage_reasons"][0]["reason"],
            "missing_terminal_usage_authority",
        )

        state, _ = _advance_budget(
            self._state(),
            canonical,
            now_monotonic=10,
        )
        self.assertEqual(state["total_tokens"], 12)
        digest_mismatch_state = self._state()
        _publish_budget_authority(
            digest_mismatch_state,
            canonical,
            terminal_digest_override="b" * 64,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "source-record digest mismatch",
        ):
            advance_experiment_budget(
                digest_mismatch_state,
                canonical["usage_event_id"],
                now_monotonic=10,
            )

        _publish_budget_authority(
            state,
            canonical,
            pretty_terminal=True,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            advance_experiment_budget(
                state,
                canonical["usage_event_id"],
                now_monotonic=10,
            )

        for name, mutate in (
            (
                "noncanonical-event-id",
                lambda usage: usage.update(
                    {"usage_event_id": "USAGE-has space"}
                ),
            ),
            (
                "incomplete-terminal-shape",
                lambda usage: usage.pop("source_artifact_path"),
            ),
        ):
            with self.subTest(name=name):
                usage = _budget_usage(
                    name,
                    input_tokens=10,
                    output_tokens=2,
                )
                mutate(usage)
                rejected, events = _advance_budget(
                    self._state(
                        max_wall_time_seconds=1,
                        initial_monotonic=10,
                    ),
                    usage,
                    now_monotonic=11,
                )
                self.assertEqual(rejected["total_tokens"], 0)
                self.assertFalse(rejected["calibration_eligible"])
                self.assertIsNone(
                    rejected["incomplete_usage_reasons"][0][
                        "usage_event_id"
                    ]
                    if name == "noncanonical-event-id"
                    else None
                )
                for event in events:
                    self._event_validator().validate(event)

    def test_budget_state_v2_rejects_unsealed_v1_state(self):
        state = self._state()
        legacy = copy.deepcopy(state)
        legacy["budget_schema_version"] = "experiment_budget_state.v1"
        legacy.pop("projection_sha256")

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "fields do not match|unsupported",
        ):
            validate_experiment_budget_state(legacy)

    def test_authority_root_symlink_and_event_log_replacement_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            real_root = parent / "real-authority"
            real_root.mkdir()
            events_path = real_root / "events.jsonl"
            events_path.touch()
            linked_root = parent / "linked-authority"
            linked_root.symlink_to(real_root, target_is_directory=True)

            with self.assertRaisesRegex(
                ExperimentBudgetIntegrityError,
                "symbolic link",
            ):
                create_experiment_budget_state(
                    "symlink-authority",
                    100,
                    100,
                    0.8,
                    authority_root=linked_root,
                    authority_events_path=linked_root / "events.jsonl",
                    initial_monotonic=0,
                )

            fifo_root = parent / "fifo-authority"
            fifo_root.mkdir()
            fifo_events = fifo_root / "events.jsonl"
            os.mkfifo(fifo_events)
            with self.assertRaisesRegex(
                ExperimentBudgetIntegrityError,
                "regular authority file",
            ):
                create_experiment_budget_state(
                    "fifo-authority",
                    100,
                    100,
                    0.8,
                    authority_root=fifo_root,
                    authority_events_path=fifo_events,
                    initial_monotonic=0,
                )

        state = self._state()
        usage = _budget_usage(
            "event-log-replacement",
            input_tokens=1,
            output_tokens=1,
        )
        events_path = (
            Path(state["authority_root"]) / state["authority_events_path"]
        )
        replacement = events_path.with_name("replacement-events.jsonl")
        replacement.touch()
        os.replace(replacement, events_path)
        _publish_budget_authority(state, usage)
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "identity changed",
        ):
            advance_experiment_budget(
                state,
                usage["usage_event_id"],
                now_monotonic=10,
            )

    def test_authority_event_log_history_cannot_be_truncated_in_place(self):
        state = self._state()
        first = _budget_usage(
            "append-only-first",
            input_tokens=10,
            output_tokens=1,
        )
        state, _ = _advance_budget(
            state,
            first,
            now_monotonic=10,
        )
        events_path = (
            Path(state["authority_root"]) / state["authority_events_path"]
        )
        original_inode = events_path.stat().st_ino
        events_path.write_text("", encoding="utf-8")
        self.assertEqual(events_path.stat().st_ino, original_inode)
        second = _budget_usage(
            "append-only-second",
            input_tokens=20,
            output_tokens=2,
        )
        _publish_budget_authority(state, second)

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "append-only prefix changed",
        ):
            advance_experiment_budget(
                state,
                second["usage_event_id"],
                now_monotonic=11,
            )

    def test_frozen_budget_drift_is_rejected_and_exhaustion_is_sticky(self):
        state = self._state()
        state, _ = _advance_budget(
            state,
            _budget_usage("exhaust", input_tokens=100, output_tokens=0),
            now_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            _budget_usage("post-exhaust", input_tokens=1, output_tokens=0),
            now_monotonic=11,
        )
        self.assertTrue(state["exhausted"])
        self.assertEqual(events, [])

        for field, value in (
            ("budget_id", "different-budget"),
            ("max_total_tokens", 1000),
            ("max_wall_time_seconds", 1000),
            ("soft_warning_ratio", 0.5),
            ("initial_monotonic", 0),
        ):
            with self.subTest(field=field):
                drifted = copy.deepcopy(state)
                drifted[field] = value
                with self.assertRaisesRegex(
                    ExperimentBudgetIntegrityError,
                    "frozen experiment budget",
                ):
                    validate_experiment_budget_state(drifted)

    def test_invalid_numeric_inputs_never_become_consumption(self):
        authority_root, authority_events_path = self._authority_paths()
        for name, kwargs in {
            "boolean-token-limit": {"max_total_tokens": True},
            "zero-token-limit": {"max_total_tokens": 0},
            "nonfinite-wall-limit": {
                "max_wall_time_seconds": float("inf")
            },
            "zero-ratio": {"soft_warning_ratio": 0},
            "unit-ratio": {"soft_warning_ratio": 1},
        }.items():
            with self.subTest(name=name):
                arguments = {
                    "budget_id": "invalid-fixture",
                    "max_total_tokens": 100,
                    "max_wall_time_seconds": 100,
                    "soft_warning_ratio": 0.8,
                    "initial_monotonic": 0,
                    "authority_root": authority_root,
                    "authority_events_path": authority_events_path,
                }
                arguments.update(kwargs)
                with self.assertRaises(ExperimentBudgetError):
                    create_experiment_budget_state(**arguments)

        invalid_usages = []
        for field, value in (
            ("input_tokens", -1),
            ("input_tokens", True),
            ("cached_input_tokens", -1),
            ("reasoning_tokens", True),
        ):
            usage = _budget_usage(
                f"invalid-{field}-{value}",
                input_tokens=1,
                output_tokens=1,
            )
            usage[field] = value
            invalid_usages.append(usage)
        inconsistent = _budget_usage(
            "inconsistent-total",
            input_tokens=1,
            output_tokens=1,
        )
        inconsistent["total_tokens"] = 3
        invalid_usages.append(inconsistent)

        for usage in invalid_usages:
            with self.subTest(usage_event_id=usage["usage_event_id"]):
                state, _ = _advance_budget(
                    self._state(),
                    usage,
                    now_monotonic=10,
                )
                self.assertEqual(state["total_tokens"], 0)
                self.assertFalse(state["calibration_eligible"])

        with self.assertRaises(ExperimentBudgetError):
            _advance_budget(
                self._state(),
                now_monotonic=float("nan"),
            )


class ExperimentProviderBudgetBoundaryTests(unittest.TestCase):
    @staticmethod
    def _runner_factory(stdout, calls):
        class FakeGatedRunner:
            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                del lifecycle, cwd, input_text, timeout_seconds, environment
                self.command = list(command)
                calls.append("constructed")

            def prepare(self):
                calls.append("prepared")
                return ExecutionGroupIdentity.not_applicable()

            def permit_and_wait(self, **_kwargs):
                calls.append("permitted")
                return ProviderExecution(self.command, 0, stdout, "")

            def abort_before_permit(self):
                calls.append("aborted")

            def cleanup_after_terminal(self):
                calls.append("cleaned")

        return FakeGatedRunner

    @staticmethod
    def _usage_stdout(input_tokens, output_tokens):
        return json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
            sort_keys=True,
        )

    def _controller(self, root, *, max_total_tokens=100):
        return create_experiment_controller(
            root,
            protocol_id="phase2-provider-boundary",
            max_total_tokens=max_total_tokens,
            max_wall_time_seconds=3600,
            soft_warning_ratio=0.8,
            scored=True,
        )

    def _call(
        self,
        root,
        controller,
        run_name,
        stdout,
        calls,
        *,
        supported=True,
    ):
        lifecycle_root = root / "runs" / run_name
        lifecycle_root.mkdir(parents=True)
        context = _model_context(
            supported=supported,
            sandbox_reference=None,
        )
        context.update(
            {
                "run_id": f"RUN-{run_name}",
                "task_id": "P2-03B",
                "attempt_id": f"ATTEMPT-{run_name}",
                "runtime_execution_session_id": f"SESSION-{run_name}",
                "lifecycle_owner_token": f"OWNER-{run_name}",
                "experiment_authority_root": str(root),
                "experiment_controller_reference": controller.reference,
            }
        )
        return ModelInvocationCall(
            lifecycle_root,
            context,
            supported=supported,
            systemd_runner_factory=self._runner_factory(stdout, calls),
        )

    @staticmethod
    def _execute(call, root):
        return call.execute(
            [str(Path(sys.executable).resolve()), "-c", "pass"],
            cwd=root,
            input_text="prompt",
            timeout_seconds=10,
        )

    def test_protocol_global_cross_run_lease_serializes_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_calls = []
            first = self._call(
                root,
                controller,
                "FIRST",
                self._usage_stdout(7, 3),
                first_calls,
            )
            first_execution = self._execute(first, root)

            second_calls = []
            second = self._call(
                root,
                controller,
                "SECOND",
                self._usage_stdout(5, 1),
                second_calls,
            )
            with self.assertRaisesRegex(
                ModelInvocationUnavailable,
                "protocol-global provider lane",
            ):
                self._execute(second, root)
            self.assertEqual(second_calls, [])
            self.assertFalse(second.lifecycle.started_path.exists())

            first.finalize("completed", first_execution)
            second_execution = self._execute(second, root)
            second.finalize("completed", second_execution)
            self.assertEqual(controller.budget_state["total_tokens"], 16)

    def test_provider_lane_inode_replacement_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_root = root / "runs" / "FIRST"
            second_root = root / "runs" / "SECOND"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            first = controller.prelaunch_admission(
                {
                    "invocation_id": "INV-FIRST",
                    "lifecycle_root": str(first_root),
                    "run_id": "RUN-FIRST",
                }
            )
            lane_path = root / "experiment-provider-lane.lock"
            lane_path.unlink()
            lane_path.touch()

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "provider lane authority identity changed",
            ):
                controller.prelaunch_admission(
                    {
                        "invocation_id": "INV-SECOND",
                        "lifecycle_root": str(second_root),
                        "run_id": "RUN-SECOND",
                    }
                )
            self.assertTrue(first.held)
            first._release()

    def test_controller_lock_and_event_inode_replacement_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            state_lock = root / "experiment-budget-controller-state.lock"
            replacement = root / "replacement-state-lock"
            replacement.touch()
            os.replace(replacement, state_lock)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "controller lock authority identity changed",
            ):
                controller.snapshot()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            state_journal = (
                root / "experiment-budget-controller-state.jsonl"
            )
            replacement = root / "replacement-state-journal"
            replacement.touch()
            os.replace(replacement, state_journal)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "state_journal authority identity changed",
            ):
                type(controller)(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            budget_events = root / "experiment-budget-events.jsonl"
            replacement = root / "replacement-budget-events"
            replacement.touch()
            os.replace(replacement, budget_events)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "budget_events authority identity changed",
            ):
                type(controller)(root)

    def test_controller_rejects_fifo_authority_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            os.mkfifo(root / "experiment-provider-lane.lock")
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "authority path is not a regular file",
            ):
                self._controller(root)

    def test_denied_prelaunch_creates_no_runner_start_or_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            admitted_calls = []
            admitted = self._call(
                root,
                controller,
                "EXHAUST",
                self._usage_stdout(8, 2),
                admitted_calls,
            )
            execution = self._execute(admitted, root)
            admitted.finalize("completed", execution)

            denied_calls = []
            denied = self._call(
                root,
                controller,
                "DENIED",
                self._usage_stdout(1, 0),
                denied_calls,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ModelInvocationUnavailable,
                    "budget exhausted",
                ):
                    self._execute(denied, root)
            self.assertEqual(denied_calls, [])
            popen.assert_not_called()
            self.assertFalse(denied.lifecycle.started_path.exists())

    def test_required_controller_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lifecycle_root = root / "runs" / "MISSING"
            lifecycle_root.mkdir(parents=True)
            calls = []
            context = _model_context(
                supported=True,
                sandbox_reference=None,
            )
            context.update(
                {
                    "run_id": "RUN-MISSING",
                    "task_id": "P2-03B",
                    "attempt_id": "ATTEMPT-MISSING",
                    "runtime_execution_session_id": "SESSION-MISSING",
                    "lifecycle_owner_token": "OWNER-MISSING",
                    "experiment_authority_root": str(root),
                    "experiment_controller_required": True,
                }
            )
            invocation = ModelInvocationCall(
                lifecycle_root,
                context,
                supported=True,
                systemd_runner_factory=self._runner_factory("", calls),
            )

            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "required experiment budget controller is unavailable",
            ):
                self._execute(invocation, root)
            self.assertEqual(calls, [])
            self.assertFalse(invocation.lifecycle.started_path.exists())

    def test_controller_reference_rejects_alternate_protocol_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_controller = self._controller(first_root)
            self._controller(second_root)
            lifecycle_root = second_root / "runs" / "MISMATCH"
            lifecycle_root.mkdir(parents=True)
            calls = []
            context = _model_context(
                supported=True,
                sandbox_reference=None,
            )
            context.update(
                {
                    "run_id": "RUN-MISMATCH",
                    "task_id": "P2-03B",
                    "attempt_id": "ATTEMPT-MISMATCH",
                    "runtime_execution_session_id": "SESSION-MISMATCH",
                    "lifecycle_owner_token": "OWNER-MISMATCH",
                    "experiment_authority_root": str(second_root),
                    "experiment_controller_reference": (
                        first_controller.reference
                    ),
                    "experiment_controller_required": True,
                }
            )
            invocation = ModelInvocationCall(
                lifecycle_root,
                context,
                supported=True,
                systemd_runner_factory=self._runner_factory("", calls),
            )

            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "reference authority changed",
            ):
                self._execute(invocation, second_root)
            self.assertEqual(calls, [])

    def test_accounting_occurs_only_after_authoritative_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "TERMINAL",
                self._usage_stdout(9, 2),
                calls,
            )
            execution = self._execute(invocation, root)

            self.assertFalse(invocation.lifecycle.terminal_path.exists())
            self.assertEqual(controller.budget_state["total_tokens"], 0)
            terminal = invocation.finalize("completed", execution)

            self.assertTrue(invocation.lifecycle.terminal_path.is_file())
            state = controller.budget_state
            self.assertEqual(state["total_tokens"], 11)
            self.assertEqual(
                state["invocation_usage_events"],
                {terminal["invocation_id"]: terminal["usage_event_id"]},
            )
            event_types = [
                json.loads(line)["event_type"]
                for line in (root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                event_types,
                [
                    "model_invocation_started",
                    "model_invocation_usage_recorded",
                ],
            )

    def test_terminal_before_projection_is_recovered_before_lane_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_calls = []
            first = self._call(
                root,
                controller,
                "RECOVERY-FIRST",
                self._usage_stdout(4, 2),
                first_calls,
            )
            first_execution = self._execute(first, root)
            first.lifecycle.finalize(
                "completed",
                stdout=first_execution.stdout,
                stderr=first_execution.stderr,
            )
            first.provider_admission._release()

            second_calls = []
            second = self._call(
                root,
                controller,
                "RECOVERY-SECOND",
                self._usage_stdout(3, 1),
                second_calls,
            )
            second_execution = self._execute(second, root)
            self.assertEqual(controller.budget_state["total_tokens"], 6)
            second.finalize("completed", second_execution)
            self.assertEqual(controller.budget_state["total_tokens"], 10)

    def test_started_orphan_is_terminalized_before_lane_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "ORPHAN",
                self._usage_stdout(4, 2),
                calls,
            )
            self._execute(invocation, root)
            invocation.provider_admission._release()

            reconciliation = reconcile_orphaned_invocation(
                invocation.lifecycle.authority_root,
                {
                    "attempt_id": "ATTEMPT-ORPHAN",
                    "lease_id": "OWNER-ORPHAN",
                    "agent_id": "agent-fixture",
                },
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "test_process_death",
                },
            )
            self.assertEqual(
                reconciliation["reconciliation_status"],
                "recovered",
            )
            self.assertTrue(invocation.lifecycle.terminal_path.is_file())

            next_root = root / "runs" / "AFTER-ORPHAN"
            next_root.mkdir(parents=True)
            with self.assertRaisesRegex(
                ExperimentProviderAdmissionDenied,
                "controller state is budget_draining",
            ):
                controller.prelaunch_admission(
                    {
                        "invocation_id": "INV-AFTER-ORPHAN",
                        "lifecycle_root": str(next_root),
                        "run_id": "RUN-AFTER-ORPHAN",
                    }
                )
            self.assertEqual(
                controller.controller_status,
                "budget_draining",
            )
            self.assertFalse(controller.budget_state["usage_complete"])

    def test_exact_exhaustion_is_sticky_and_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            calls = []
            invocation = self._call(
                root,
                controller,
                "EXACT",
                self._usage_stdout(6, 4),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)

            state = controller.budget_state
            self.assertEqual(state["total_tokens"], 10)
            self.assertEqual(state["overshoot_tokens"], 0)
            self.assertTrue(state["exhausted"])
            self.assertEqual(
                [event["event_kind"] for event in state["events"]],
                ["warning", "exhaustion"],
            )
            self.assertEqual(
                controller.controller_status,
                "budget_draining",
            )
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "cannot be reset or extended",
            ):
                self._controller(root, max_total_tokens=11)
            observed = controller.observe_boundary(
                "post_integration",
                scheduler_inflight=0,
                integration_active=False,
            )
            self.assertEqual(
                observed["controller_status"],
                "budget_stopped",
            )
            with self.assertRaisesRegex(
                ExperimentControllerError,
                "only interrupted",
            ):
                controller.resume_interrupted()

    def test_valid_state_snapshot_rollback_restores_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            state_path = root / "experiment-budget-controller-state.json"
            pre_usage_snapshot = state_path.read_bytes()
            calls = []
            invocation = self._call(
                root,
                controller,
                "ROLLBACK",
                self._usage_stdout(8, 2),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)
            self.assertTrue(controller.budget_state["exhausted"])

            state_path.write_bytes(pre_usage_snapshot)
            recovered = type(controller)(root)
            self.assertEqual(recovered.budget_state["total_tokens"], 10)
            self.assertTrue(recovered.budget_state["exhausted"])
            self.assertEqual(
                recovered.controller_status,
                "budget_draining",
            )

            denied = self._call(
                root,
                recovered,
                "ROLLBACK-DENIED",
                self._usage_stdout(1, 0),
                [],
            )
            with self.assertRaisesRegex(
                ModelInvocationUnavailable,
                "budget exhausted",
            ):
                self._execute(denied, root)

    def test_state_journal_truncation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            controller.interrupt()
            journal_path = (
                root / "experiment-budget-controller-state.jsonl"
            )
            first_checkpoint = journal_path.read_bytes().splitlines()[0]
            journal_path.write_bytes(first_checkpoint + b"\n")

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "state is ahead of its append-first journal",
            ):
                type(controller)(root)

    def test_torn_state_journal_tail_recovers_last_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            controller.interrupt()
            journal_path = (
                root / "experiment-budget-controller-state.jsonl"
            )
            committed = journal_path.read_bytes()
            journal_path.write_bytes(committed + b'{"schema_version":')

            recovered = type(controller)(root)

            self.assertEqual(recovered.controller_status, "interrupted")
            self.assertEqual(journal_path.read_bytes(), committed)

    def test_interruption_resume_preserves_original_remaining_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "INTERRUPTED",
                self._usage_stdout(12, 3),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)
            controller.interrupt()

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "extension or reduction",
            ):
                controller.resume_interrupted(max_total_tokens=101)
            resumed = controller.resume_interrupted(
                reference=controller.reference,
                max_total_tokens=100,
                max_wall_time_seconds=3600,
            )
            self.assertEqual(resumed["controller_status"], "active")
            self.assertEqual(
                resumed["budget_state"]["total_tokens"],
                15,
            )


class _SchedulerMonotonic:
    def __init__(self, value=None):
        self.value = time.monotonic() if value is None else value

    def __call__(self):
        self.value = max(self.value, time.monotonic())
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _SchedulerClock:
    def now(self):
        return "2026-07-27T19:00:00Z"


class TwoPhaseSchedulerExperimentBoundaryTests(unittest.TestCase):
    def _controller(
        self,
        output_dir,
        monotonic,
        *,
        max_total_tokens=100,
        max_wall_time_seconds=60,
    ):
        return create_experiment_controller(
            output_dir,
            protocol_id="phase2-scheduler-boundary",
            max_total_tokens=max_total_tokens,
            max_wall_time_seconds=max_wall_time_seconds,
            soft_warning_ratio=0.8,
            scored=True,
            initial_monotonic=monotonic.value,
            monotonic=monotonic,
        )

    def _scheduler(
        self,
        root,
        controller,
        monotonic,
        *,
        write_scope=None,
        project_root=None,
        verification_command=None,
        commit_verified_integration=False,
        resume_interrupted_experiment=False,
        invocation_fence_assessor=None,
        task_overrides=None,
        max_inflight=1,
        task_count=1,
        output_dir=None,
    ):
        output_dir = Path(output_dir or controller.root)
        output_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "task_id": "P2-03B-SCHEDULER",
            "milestone_id": "M0",
            "objective": "Exercise scheduler experiment boundaries.",
            "backlog_status": "ready",
            "risk_target": "L0",
            "depends_on": [],
            "read_scope": ["."],
            "write_scope": list(write_scope or []),
            "required_role": "implementation_worker",
            "blockers": [],
        }
        task.update(task_overrides or {})
        backlog_path = root / "backlog.json"
        tasks = [task]
        for index in range(2, task_count + 1):
            additional = copy.deepcopy(task)
            additional["task_id"] = f"P2-03B-SCHEDULER-{index}"
            tasks.append(additional)
        backlog_path.write_text(
            json.dumps(
                {"backlog_id": "BL-P2-03B", "items": tasks},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        agent_pool_path = root / "agent_pool.json"
        agent_pool_path.write_text(
            json.dumps(
                {
                    "pool_id": "phase2-scheduler-pool",
                    "scheduler_agent_id": "agent-scheduler",
                    "updated_at": "2026-07-27T19:00:00Z",
                    "agents": [
                        {
                            "agent_id": (
                                "agent-implementation"
                                if index == 1
                                else f"agent-implementation-{index}"
                            ),
                            "role": "implementation_worker",
                            "status": "idle",
                            "model_profile": "test",
                            "runtime_adapter": "codex",
                            "subscriptions": [],
                            "inbox_path": (
                                "mailboxes/agent-implementation/inbox.jsonl"
                                if index == 1
                                else f"mailboxes/agent-implementation-{index}/inbox.jsonl"
                            ),
                            "outbox_path": (
                                "mailboxes/agent-implementation/outbox.jsonl"
                                if index == 1
                                else f"mailboxes/agent-implementation-{index}/outbox.jsonl"
                            ),
                            "lease": {
                                "lease_id": None,
                                "task_id": None,
                                "expires_at": None,
                            },
                            "owned_artifacts": [],
                            "last_event_id": None,
                            "memory_summary_path": None,
                        }
                        for index in range(1, task_count + 1)
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=_SchedulerClock(),
            project_root=project_root,
            max_inflight=max_inflight,
            integrate_accepted_patch=project_root is not None,
            integration_verification_command=verification_command,
            commit_verified_integration=commit_verified_integration,
            experiment_controller_reference=controller.reference,
            experiment_controller_required=True,
            resume_interrupted_experiment=resume_interrupted_experiment,
            experiment_controller_monotonic=monotonic,
            invocation_fence_assessor=invocation_fence_assessor,
        )

    @classmethod
    def _append_result(
        cls,
        inflight,
        changed_files,
        *,
        output=None,
        publish_terminal=True,
    ):
        if publish_terminal:
            cls._publish_zero_usage_terminal(inflight)
        record = {
            "message_id": f"RESULT-{inflight['message_id']}",
            "from_agent": inflight["agent_id"],
            "to_agent": "agent-scheduler",
            "message_type": "runtime_result",
            "correlation_id": inflight["correlation_id"],
            "created_at": "2026-07-27T19:00:01Z",
            "payload": {
                "source_message_id": inflight["message_id"],
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "lease_id": inflight["lease_id"],
                "result_status": "completed",
                "changed_files": list(changed_files),
                "output": output or {"test": "phase2-scheduler-boundary"},
            },
        }
        outbox_path = Path(inflight["outbox_path"])
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with outbox_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")

    @staticmethod
    def _publish_zero_usage_terminal(inflight):
        output_dir = Path(inflight["step_dir"]).parents[1]
        matching_terminals = [
            terminal_path
            for terminal_path in output_dir.glob(
                "model_invocations/*/terminal.json"
            )
            if json.loads(
                terminal_path.read_text(encoding="utf-8")
            ).get("attempt_id")
            == inflight["attempt_id"]
        ]
        if matching_terminals:
            return
        inbox_paths = list(
            Path(inflight["step_dir"]).glob(
                "mailboxes/*/inbox.jsonl"
            )
        )
        if len(inbox_paths) != 1:
            raise AssertionError("expected one scheduler inbox fixture")
        message = json.loads(
            inbox_paths[0].read_text(encoding="utf-8").splitlines()[0]
        )
        context = _model_context(
            supported=True,
            sandbox_reference=None,
        )
        context.update(invocation_context_from_message(message))
        context["coverage_class"] = "supported_model_invocation"
        context["backend"] = "codex"
        calls = []
        invocation = ModelInvocationCall(
            output_dir,
            context,
            supported=True,
            systemd_runner_factory=(
                ExperimentProviderBudgetBoundaryTests._runner_factory(
                    ExperimentProviderBudgetBoundaryTests._usage_stdout(
                        0,
                        0,
                    ),
                    calls,
                )
            ),
        )
        execution = ExperimentProviderBudgetBoundaryTests._execute(
            invocation,
            output_dir,
        )
        invocation.finalize("completed", execution)

    @staticmethod
    def _init_repo(repo):
        repo.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "config", "user.email", "agentteam@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "AgentTeam Test"],
            cwd=repo,
            check=True,
        )
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _head(repo, ref="HEAD"):
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _invocation_for_inflight(
        output_dir,
        controller,
        inflight,
        stdout,
        calls,
    ):
        context = _model_context(supported=True, sandbox_reference=None)
        context.update(
            {
                "run_id": "RUN-TWO-PHASE-SCHEDULER",
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "runtime_execution_session_id": inflight[
                    "runtime_session_id"
                ],
                "lifecycle_owner_token": inflight["lease_id"],
                "agent_id": inflight["agent_id"],
                "experiment_authority_root": str(controller.root),
                "experiment_controller_reference": controller.reference,
                "experiment_controller_required": True,
            }
        )
        return ModelInvocationCall(
            output_dir,
            context,
            supported=True,
            systemd_runner_factory=(
                ExperimentProviderBudgetBoundaryTests._runner_factory(
                    stdout,
                    calls,
                )
            ),
        )

    def test_scheduler_denies_worker_dispatch_after_budget_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(root, controller, monotonic)
            monotonic.advance(2)

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_count"], 0)
            self.assertEqual(dispatch["dispatch_status"], "budget_stopped")
            self.assertEqual(scheduler.state["inflight_attempts"], [])
            self.assertFalse((output_dir / "steps").exists())
            self.assertEqual(controller.controller_status, "budget_stopped")
            self.assertTrue(controller.budget_state["exhausted"])

    def test_scheduler_injects_only_trusted_controller_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                task_overrides={
                    "experiment_authority_root": str(root / "untrusted"),
                    "experiment_controller_reference": {
                        "schema_version": "untrusted-reference"
                    },
                    "experiment_controller_required": False,
                },
            )

            dispatch = scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            inbox = (
                Path(inflight["step_dir"])
                / "mailboxes"
                / "agent-implementation"
                / "inbox.jsonl"
            )
            message = json.loads(inbox.read_text(encoding="utf-8").splitlines()[0])
            payload = message["payload"]

            self.assertEqual(dispatch["dispatch_count"], 1)
            self.assertEqual(
                payload["experiment_controller_reference"],
                controller.reference,
            )
            self.assertTrue(payload["experiment_controller_required"])
            self.assertEqual(
                payload["experiment_authority_root"],
                str(controller.root),
            )
            persisted = json.loads(scheduler.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["experiment_controller_reference"],
                controller.reference,
            )

    def test_experiment_scheduler_serializes_worker_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                max_inflight=2,
                task_count=2,
            )

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_count"], 1)
            self.assertEqual(len(scheduler.state["inflight_attempts"]), 1)
            self.assertEqual(
                scheduler.state["backlog"]["items"][1][
                    "backlog_status"
                ],
                "ready",
            )

    def test_mode_scheduler_allocates_registered_independent_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            self._init_repo(repository)
            commit = self._head(repository)
            tree = self._head(repository, "HEAD^{tree}")
            object_format = _git(
                repository,
                "rev-parse",
                "--show-object-format",
            ).stdout.strip()
            monotonic = _SchedulerMonotonic()
            authority_root = root / "run-authority"
            authority_root.mkdir(mode=0o700)
            controller = create_experiment_controller(
                root / "protocol-controller",
                protocol_id="phase2-mode-scheduler",
                max_total_tokens=100,
                max_wall_time_seconds=60,
                soft_warning_ratio=0.8,
                scored=False,
                protocol_sha256="1" * 64,
                operator_limits={
                    "expected_operator_action": 2,
                    "corrective_intervention": 1,
                    "decision_escalation": 1,
                },
                initial_monotonic=monotonic.value,
                monotonic=monotonic,
            )
            model_policy = {
                "backend": "codex",
                "codex_cli_version": "codex-test-v1",
                "model": "codex-test-model",
                "reasoning_profile": "high",
                "service_configuration_sha256": "2" * 64,
                "sandbox_policy": "workspace-write",
                "permission_policy": "never",
                "network_policy": "disabled",
                "tool_allowlist": ["exec_command", "apply_patch"],
                "max_inflight_model_invocations": 1,
            }
            credential = root / "credential.json"
            credential.write_text("{}\n", encoding="utf-8")
            canary = root / "canary"
            canary.write_text("scheduler canary\n", encoding="utf-8")
            configuration = {
                "runtime_views": [
                    {"source": path, "target": path}
                    for path in ("/usr", "/lib", "/lib64", "/bin")
                    if Path(path).exists()
                ],
                "library_views": [],
                "credential_mounts": [
                    {
                        "source": str(credential),
                        "target": (
                            "/run/agentteam-credentials/provider.json"
                        ),
                    }
                ],
                "environment": {},
                "canary_path": str(canary),
            }
            publish_experiment_mode_authority(
                authority_root,
                experiment_run_id="RUN-MODE-SCHEDULER",
                protocol_sha256="1" * 64,
                run_manifest_sha256="3" * 64,
                mode="agentteam_direct",
                model_policy=model_policy,
                controller_reference=controller.reference,
                sandbox_configuration_sha256=canonical_json_sha256(
                    configuration
                ),
            )
            (authority_root / "experiment-runtime-context.json").write_text(
                json.dumps(
                    {
                        "schema_version": "experiment_runtime_context.v1",
                        "experiment_run_id": "RUN-MODE-SCHEDULER",
                        "protocol_sha256": "1" * 64,
                        "run_manifest_sha256": "3" * 64,
                        "authority_root": str(authority_root),
                        "mode": "agentteam_direct",
                        "repository_identity": {
                            "source": str(repository),
                            "commit": commit,
                            "tree": tree,
                            "git_object_format": object_format,
                        },
                        "controller_reference": controller.reference,
                        "controller_required": True,
                        "independent_attempt_workspaces": True,
                        "model_policy": model_policy,
                        "sandbox_configuration": configuration,
                        "sandbox_configuration_sha256": (
                            canonical_json_sha256(configuration)
                        ),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with patch(
                "agentteam_runtime.experiment_sandbox."
                "probe_gold_canary_denial",
                side_effect=_successful_namespace_probe,
            ):
                scheduler = self._scheduler(
                    root,
                    controller,
                    monotonic,
                    project_root=repository,
                    max_inflight=1,
                    output_dir=authority_root,
                )
                dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_count"], 1)
            inflight = scheduler.state["inflight_attempts"][0]
            workspace = Path(inflight["worktree_path"])
            integration_workspace = Path(
                scheduler.state["integration_baseline"][
                    "integration_baseline_worktree_path"
                ]
            )
            common_dirs = {
                Path(
                    _git(
                        path,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ).stdout.strip()
                ).resolve()
                for path in (
                    repository,
                    workspace,
                    integration_workspace,
                )
            }
            self.assertEqual(len(common_dirs), 3)
            self.assertNotEqual(
                Path(
                    _git(
                        repository,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ).stdout.strip()
                ).resolve(),
                Path(
                    _git(
                        workspace,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ).stdout.strip()
                ).resolve(),
            )
            inbox = next(
                Path(inflight["step_dir"]).glob(
                    "mailboxes/*/inbox.jsonl"
                )
            )
            payload = json.loads(
                inbox.read_text(encoding="utf-8").splitlines()[0]
            )["payload"]
            self.assertTrue(payload["experiment_sandbox_required"])
            self.assertEqual(
                payload["model"],
                model_policy["model"],
            )
            self.assertEqual(
                payload["reasoning_profile"],
                model_policy["reasoning_profile"],
            )
            invocation_context = invocation_context_from_message(
                {"payload": payload},
                model=model_policy["model"],
            )
            command = _with_codex_reasoning_profile(
                [
                    "codex",
                    "exec",
                    "-m",
                    model_policy["model"],
                    "-",
                ],
                invocation_context["reasoning_profile"],
            )
            _validate_registered_codex_command(
                command,
                model_policy,
            )
            self.assertTrue(
                Path(
                    payload["model_invocation_authority_root"]
                ).is_dir()
            )

    def test_outbox_result_waits_for_provider_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                invocation_fence_assessor=lambda _start: {
                    "fence_status": "live_pinned",
                    "proof": "test_provider_still_live",
                },
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(1, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            self._append_result(
                inflight,
                [],
                publish_terminal=False,
            )
            waiting = scheduler.collect_ready_results()

            self.assertEqual(waiting["collected_count"], 0)
            self.assertEqual(waiting["inflight_count"], 1)
            invocation.finalize("completed", execution)

            collected = scheduler.collect_ready_results()

            self.assertEqual(collected["collected_count"], 1)
            self.assertEqual(collected["inflight_count"], 0)
            self.assertEqual(controller.budget_state["total_tokens"], 2)

    def test_outbox_without_provider_start_remains_inflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            self._append_result(
                inflight,
                [],
                publish_terminal=False,
            )
            outbox_path = Path(inflight["outbox_path"])
            malformed = json.loads(
                outbox_path.read_text(encoding="utf-8")
            )
            malformed["payload"]["changed_files"] = None
            malformed["payload"]["output"] = None
            outbox_path.write_text(
                json.dumps(malformed, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            waiting = scheduler.collect_ready_results()

            self.assertEqual(waiting["collected_count"], 0)
            self.assertEqual(waiting["inflight_count"], 1)
            self.assertEqual(
                inflight["invocation_reconciliation"][
                    "reconciliation_status"
                ],
                "no_invocation",
            )
            inflight["lease_expires_at"] = "2026-07-27T18:59:59Z"

            expired = scheduler.collect_ready_results()

            self.assertEqual(expired["collected_count"], 1)
            self.assertEqual(expired["inflight_count"], 0)
            result = expired["results"][0]
            self.assertEqual(
                result["runtime_output"]["error"],
                "lease_expired_without_provider_start",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "result_status"
                ],
                "completed",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "changed_files_type"
                ],
                "NoneType",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "output_type"
                ],
                "NoneType",
            )

    def test_direct_collect_rejects_unfinished_integration_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            self._append_result(inflight, [])
            scheduler.state["integration_active"] = True
            scheduler.state["integration_attempt_id"] = inflight[
                "attempt_id"
            ]
            scheduler._write_state()

            blocked = scheduler.collect_ready_results()

            self.assertEqual(
                blocked["collect_status"],
                "integration_recovery_required",
            )
            self.assertEqual(blocked["collected_count"], 0)
            self.assertEqual(blocked["inflight_count"], 1)
            self.assertEqual(scheduler.state["steps"], [])

    def test_scheduler_drains_inflight_and_exposes_terminal_overshoot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_total_tokens=10,
            )
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(8, 5),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            invocation.finalize("completed", execution)
            self._append_result(inflight, [])

            collected = scheduler.collect_ready_results()

            self.assertEqual(collected["collected_count"], 1)
            self.assertEqual(collected["inflight_count"], 0)
            self.assertEqual(
                collected["experiment_controller_status"],
                "budget_stopped",
            )
            budget = collected["experiment_budget_state"]
            self.assertEqual(budget["total_tokens"], 13)
            self.assertEqual(budget["overshoot_tokens"], 3)
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(calls, ["constructed", "prepared", "permitted", "cleaned"])

    def test_preintegration_exhaustion_preserves_patch_and_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
                commit_verified_integration=True,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "accepted patch\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            monotonic.advance(2)

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(inflight["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_status"], "preserved")
            self.assertEqual(result["integration_queue_status"], "pending")
            self.assertEqual(
                result["completion_policy"],
                "accepted_patch_preserved_budget_stop",
            )
            self.assertTrue(Path(result["patch_path"]).is_file())
            self.assertIn(
                "accepted patch",
                Path(result["patch_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(self._head(baseline), source_head)
            self.assertFalse((baseline / "feature.txt").exists())
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")
            stopped_tick = scheduler.tick()
            self.assertEqual(stopped_tick["tick_status"], "budget_stopped")
            self.assertEqual(self._head(baseline), source_head)

    def test_budget_stop_is_deferred_through_successful_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('feature.txt').is_file()",
                ],
                commit_verified_integration=True,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "durable integration\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            observations = []
            initial_monotonic = monotonic.value
            original_observe = scheduler.experiment_controller.observe_boundary
            original_verify = two_phase_scheduler_module.run_integration_verification

            def record_observation(boundary, **kwargs):
                observations.append(
                    (boundary, kwargs["integration_active"], monotonic.value)
                )
                return original_observe(boundary, **kwargs)

            def cross_budget(*args, **kwargs):
                monotonic.advance(2)
                return original_verify(*args, **kwargs)

            with patch.object(
                scheduler.experiment_controller,
                "observe_boundary",
                side_effect=record_observation,
            ), patch.object(
                two_phase_scheduler_module,
                "load_experiment_controller",
                return_value=scheduler.experiment_controller,
            ), patch.object(
                two_phase_scheduler_module,
                "run_integration_verification",
                side_effect=cross_budget,
            ):
                collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(result["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_baseline_commit_status"], "committed")
            self.assertNotEqual(self._head(baseline), source_head)
            self.assertTrue((baseline / "feature.txt").is_file())
            integration_observations = [
                item
                for item in observations
                if item[0] != "pre_provider_launch"
            ]
            self.assertEqual(
                [
                    (boundary, active)
                    for boundary, active, _now
                    in integration_observations
                ],
                [
                    ("pre_integration", False),
                    ("post_integration", False),
                    ("post_integration", False),
                ],
            )
            self.assertGreaterEqual(
                integration_observations[0][2],
                initial_monotonic,
            )
            self.assertGreaterEqual(
                integration_observations[1][2],
                integration_observations[0][2] + 2,
            )
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")
            self.assertFalse(scheduler.state["integration_active"])

    def test_budget_stop_is_deferred_through_integration_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "raise SystemExit(7)"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "must roll back\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            original_verify = two_phase_scheduler_module.run_integration_verification

            def cross_budget(*args, **kwargs):
                monotonic.advance(2)
                return original_verify(*args, **kwargs)

            with patch.object(
                two_phase_scheduler_module,
                "run_integration_verification",
                side_effect=cross_budget,
            ):
                collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(result["integration_baseline_worktree_path"])
            event_types = [
                json.loads(line)["event_type"]
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_baseline_rollback_status"], "reset")
            self.assertEqual(self._head(baseline), source_head)
            self.assertFalse((baseline / "feature.txt").exists())
            self.assertIn("integration_blocked", event_types)
            self.assertIn("integration_baseline_commit_evaluated", event_types)
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")

    def test_tick_resumes_interrupted_controller_before_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "resume before integration\n",
                encoding="utf-8",
            )
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(1, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            invocation.finalize("completed", execution)
            self._append_result(inflight, ["feature.txt"])
            controller.interrupt()

            resumed = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
                resume_interrupted_experiment=True,
            )
            tick = resumed.tick()
            result = tick["collect"]["results"][0]

            self.assertEqual(tick["collect"]["collected_count"], 1)
            self.assertEqual(
                result["pre_integration_controller_observation"][
                    "controller_status"
                ],
                "active",
            )
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(
                result["integration_verification_status"],
                "passed",
            )
            self.assertNotEqual(
                result["completion_policy"],
                "accepted_patch_preserved_budget_stop",
            )

    def test_completed_integration_recovery_does_not_repeat_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            scheduler.dispatch_ready()
            inflight = copy.deepcopy(
                scheduler.state["inflight_attempts"][0]
            )
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "commit once\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            first = scheduler.collect_ready_results()
            first_head = self._head(
                first["results"][0][
                    "integration_baseline_worktree_path"
                ]
            )
            self.assertEqual(len(scheduler.state["steps"]), 1)

            scheduler.state["integration_active"] = True
            scheduler.state["integration_attempt_id"] = inflight[
                "attempt_id"
            ]
            scheduler.state["inflight_attempts"] = [inflight]
            scheduler._write_state()

            recovered = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            tick = recovered.tick()

            self.assertEqual(tick["collect"]["collected_count"], 1)
            self.assertEqual(len(recovered.state["steps"]), 1)
            self.assertEqual(recovered.state["inflight_attempts"], [])
            self.assertFalse(recovered.state["integration_active"])
            self.assertEqual(
                self._head(
                    recovered.state["integration_baseline"][
                        "integration_baseline_worktree_path"
                    ]
                ),
                first_head,
            )

    def test_interrupted_scheduler_reconciles_before_original_budget_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(4, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            frozen_budget_sha256 = controller.reference["frozen_budget_sha256"]
            controller.interrupt()

            blocked = self._scheduler(
                root,
                controller,
                monotonic,
                resume_interrupted_experiment=True,
                invocation_fence_assessor=lambda _start: {
                    "fence_status": "live_pinned",
                    "proof": "deterministic_live_worker",
                },
            )
            blocked_dispatch = blocked.dispatch_ready()
            self.assertEqual(
                blocked_dispatch["dispatch_status"],
                "invocation_reconciliation_pending",
            )
            self.assertEqual(controller.controller_status, "interrupted")

            invocation.finalize("completed", execution)
            resumed = self._scheduler(
                root,
                controller,
                monotonic,
                resume_interrupted_experiment=True,
            )
            resumed_dispatch = resumed.dispatch_ready()

            self.assertEqual(resumed_dispatch["dispatch_status"], "at_capacity")
            self.assertEqual(controller.controller_status, "active")
            self.assertEqual(controller.budget_state["total_tokens"], 5)
            self.assertEqual(
                controller.reference["frozen_budget_sha256"],
                frozen_budget_sha256,
            )
            recovered_collection = resumed.collect_ready_results()
            self.assertEqual(recovered_collection["collected_count"], 1)
            self.assertEqual(recovered_collection["inflight_count"], 0)
            alternate_root = root / "alternate-controller"
            alternate = self._controller(alternate_root, monotonic)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "reference changed on restart",
            ):
                TwoPhaseFileScheduler(
                    root / "agent_pool.json",
                    root / "backlog.json",
                    output_dir,
                    clock=_SchedulerClock(),
                    experiment_controller_reference=alternate.reference,
                    experiment_controller_required=True,
                    experiment_controller_monotonic=monotonic,
                )


class ExperimentOperatorActionLedgerTests(unittest.TestCase):
    LIMITS = {
        "expected_operator_action": 2,
        "corrective_intervention": 1,
        "decision_escalation": 1,
    }

    @staticmethod
    def _controller(root):
        protocol = _protocol()
        return create_experiment_controller(
            root,
            protocol_id=protocol["experiment_id"],
            protocol_sha256=canonical_json_sha256(protocol),
            operator_limits=protocol["operator_limits"],
            max_total_tokens=100,
            max_wall_time_seconds=3600,
            soft_warning_ratio=0.8,
            scored=True,
        )

    @staticmethod
    def _event(event_type, time_value, payload):
        digest = hashlib.sha256(
            json.dumps(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "time": time_value,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "event_id": f"EVENT-{digest}",
            "event_type": event_type,
            "time": time_value,
            "payload": payload,
        }

    @staticmethod
    def _manifest(mode="single_codex", key="operator-ledger-001"):
        return build_experiment_run_manifest(
            _protocol(),
            mode=mode,
            repetition_index=0,
            stable_request_key=key,
        )

    @staticmethod
    def _bind_target_run(controller, manifest, run_dir):
        run_dir = Path(run_dir).resolve()
        state_path = run_dir / "state" / "two_phase_scheduler_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {}
        )
        state.update(
            {
                "experiment_controller_reference": controller.reference,
                "experiment_run_id": manifest["experiment_run_id"],
                "experiment_run_manifest_sha256": (
                    canonical_json_sha256(manifest)
                ),
                "experiment_target_path_sha256": hashlib.sha256(
                    str(run_dir).encode("utf-8")
                ).hexdigest(),
            }
        )
        state_path.write_text(
            json.dumps(state, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _prepare_operator_target(self, controller, manifest, request):
        run_dir = (
            controller.root.parent
            / f"{controller.root.name}-operator-targets"
            / manifest["experiment_run_id"]
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        self._bind_target_run(controller, manifest, run_dir)
        events_path = run_dir / "events.jsonl"
        existing = (
            [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line
            ]
            if events_path.exists()
            else []
        )
        if not any(
            event.get("event_id") == request.get("event_id")
            and event == request
            for event in existing
        ):
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(request, sort_keys=True) + "\n")
        return run_dir

    def _record(
        self,
        controller,
        *,
        event_type="permission_request_resolved",
        manifest=None,
        request=None,
        response=None,
    ):
        request = request or self._event(
            "permission_request_required",
            "2026-07-27T00:00:00Z",
            {
                "request_id": "PERM-001",
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "secret": "must-not-be-retained",
            },
        )
        if (
            response is None
            and event_type
            not in {
                "run_stop_requested",
                "run_resume_requested",
                "corrective_guidance_received",
            }
        ):
            response = self._event(
                event_type,
                "2026-07-27T00:00:01Z",
                {
                    "request_id": "PERM-001",
                    "decision": "approved",
                    "secret": "also-must-not-be-retained",
                },
            )
        manifest = manifest or self._manifest()
        run_dir = self._prepare_operator_target(
            controller,
            manifest,
            request,
        )
        return record_experiment_operator_event(
            controller,
            protocol=_protocol(),
            run_manifest=manifest,
            run_dir=run_dir,
            event_type=event_type,
            request_event=request,
            response_event=response,
        )

    def test_supported_inputs_map_to_closed_vocabulary_without_raw_content(self):
        cases = (
            (
                "operator_answer_received",
                "manual_gate_required",
                "decision_escalation",
                False,
            ),
            (
                "permission_request_resolved",
                "permission_request_required",
                "expected_operator_action",
                False,
            ),
            (
                "run_stop_requested",
                "run_stop_requested",
                "corrective_intervention",
                True,
            ),
            (
                "run_resume_requested",
                "run_resume_requested",
                "corrective_intervention",
                True,
            ),
            (
                "corrective_guidance_received",
                "corrective_guidance_received",
                "corrective_intervention",
                True,
            ),
        )
        for index, (
            event_type,
            request_type,
            action_class,
            intervention,
        ) in enumerate(cases):
            with self.subTest(event_type=event_type):
                with tempfile.TemporaryDirectory() as tmp:
                    controller = self._controller(Path(tmp) / "controller")
                    correlation_fields = {
                        "manual_gate_required": "question_id",
                        "permission_request_required": "request_id",
                    }
                    correlation_field = correlation_fields.get(
                        request_type
                    )
                    correlation_value = f"CORRELATION-{index}"
                    request_payload = {
                        "task_id": f"TASK-{index}",
                        "attempt_id": f"ATTEMPT-{index}",
                        "message": "secret request body",
                    }
                    response_payload = {
                        "message": "secret response body"
                    }
                    if correlation_field is not None:
                        request_payload[correlation_field] = (
                            correlation_value
                        )
                        response_payload[correlation_field] = (
                            correlation_value
                        )
                    request = self._event(
                        request_type,
                        "2026-07-27T00:00:00Z",
                        request_payload,
                    )
                    response = self._event(
                        event_type,
                        "2026-07-27T00:00:01Z",
                        response_payload,
                    ) if event_type not in {
                        "run_stop_requested",
                        "run_resume_requested",
                        "corrective_guidance_received",
                    } else None
                    recorded = self._record(
                        controller,
                        event_type=event_type,
                        request=request,
                        response=response,
                    )
                    entry = recorded["entry"]

                    self.assertEqual(entry["action_class"], action_class)
                    self.assertIs(
                        entry["counts_as_intervention"],
                        intervention,
                    )
                    serialized = json.dumps(entry, sort_keys=True)
                    self.assertNotIn("secret request body", serialized)
                    self.assertNotIn("secret response body", serialized)
                    self.assertEqual(len(entry["request_digest"]), 64)
                    if response is None:
                        self.assertIsNone(entry["response_digest"])
                    else:
                        self.assertEqual(len(entry["response_digest"]), 64)
                    self.assertIs(
                        validate_experiment_operator_action(entry),
                        entry,
                    )

    def test_replay_is_idempotent_and_conflicting_response_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            first = self._record(controller)
            replayed = self._record(controller)

            self.assertEqual(first["record_status"], "recorded")
            self.assertEqual(replayed["record_status"], "already_recorded")
            self.assertEqual(replayed["projection"]["action_count"], 1)

            changed_response = self._event(
                "permission_request_resolved",
                "2026-07-27T00:00:01Z",
                {"request_id": "PERM-001", "decision": "denied"},
            )
            with self.assertRaisesRegex(
                ExperimentLedgerIntegrityError,
                "replay conflicts",
            ):
                self._record(controller, response=changed_response)

    def test_frozen_equal_limits_apply_to_every_mode_and_exhaust_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "controller"
            controller = self._controller(root)
            first = self._record(
                controller,
                manifest=self._manifest(
                    "single_codex",
                    "operator-ledger-single",
                ),
            )
            direct = self._record(
                controller,
                manifest=self._manifest(
                    "agentteam_direct",
                    "operator-ledger-direct",
                ),
            )

            self.assertEqual(
                first["projection"]["operator_action_counts"][
                    "expected_operator_action"
                ],
                1,
            )
            self.assertEqual(
                direct["projection"]["operator_action_counts"][
                    "expected_operator_action"
                ],
                1,
            )
            changed_limits = {
                **self.LIMITS,
                "expected_operator_action": 3,
            }
            changed_protocol = {
                **_protocol(),
                "operator_limits": changed_limits,
            }
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "differs from frozen.*authority",
            ):
                controller.record_operator_action(
                    protocol=changed_protocol,
                    run_manifest=build_experiment_run_manifest(
                        changed_protocol,
                        mode="agentteam_full",
                        repetition_index=0,
                        stable_request_key="operator-ledger-full",
                    ),
                    run_dir=Path(tmp) / "unbound-target",
                    request_source="permission_request",
                    request={
                        "event_type": "permission_request_required",
                        "request_id": "PERM-002",
                    },
                    response={
                        "event_type": "permission_request_resolved",
                        "decision": "approved",
                    },
                    requested_at="2026-07-27T00:00:00Z",
                    answered_at="2026-07-27T00:00:01Z",
                )

            one_limit = {
                **self.LIMITS,
                "expected_operator_action": 1,
            }
            limited_protocol = {
                **_protocol(),
                "operator_limits": one_limit,
            }
            isolated = create_experiment_controller(
                Path(tmp) / "isolated",
                protocol_id=limited_protocol["experiment_id"],
                protocol_sha256=canonical_json_sha256(
                    limited_protocol
                ),
                operator_limits=one_limit,
                max_total_tokens=100,
                max_wall_time_seconds=3600,
                soft_warning_ratio=0.8,
                scored=True,
            )
            limited_manifest = build_experiment_run_manifest(
                limited_protocol,
                mode="agentteam_full",
                repetition_index=0,
                stable_request_key="operator-ledger-limited",
            )
            first_request = self._event(
                "permission_request_required",
                "2026-07-27T00:00:00Z",
                {"request_id": "PERM-001"},
            )
            limited_run_dir = self._prepare_operator_target(
                isolated,
                limited_manifest,
                first_request,
            )
            isolated.record_operator_action(
                protocol=limited_protocol,
                run_manifest=limited_manifest,
                run_dir=limited_run_dir,
                request_source="permission_request",
                request=first_request,
                response=self._event(
                    "permission_request_resolved",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-001",
                        "decision": "approved",
                    },
                ),
                requested_at="2026-07-27T00:00:00Z",
                answered_at="2026-07-27T00:00:01Z",
            )
            second_request = self._event(
                "permission_request_required",
                "2026-07-27T00:00:02Z",
                {"request_id": "PERM-002"},
            )
            self._prepare_operator_target(
                isolated,
                limited_manifest,
                second_request,
            )
            with self.assertRaises(
                ExperimentOperatorActionLimitExceeded
            ):
                isolated.record_operator_action(
                    protocol=limited_protocol,
                    run_manifest=limited_manifest,
                    run_dir=limited_run_dir,
                    request_source="permission_request",
                    request=second_request,
                    response=self._event(
                        "permission_request_resolved",
                        "2026-07-27T00:00:03Z",
                        {
                            "request_id": "PERM-002",
                            "decision": "approved",
                        },
                    ),
                    requested_at="2026-07-27T00:00:02Z",
                    answered_at="2026-07-27T00:00:03Z",
                )

    def test_run_identity_cannot_change_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            self._record(
                controller,
                manifest=self._manifest(
                    "single_codex",
                    "operator-ledger-mode-bound",
                ),
            )
            request = self._event(
                "permission_request_required",
                "2026-07-27T00:00:02Z",
                {"request_id": "PERM-002"},
            )
            response = self._event(
                "permission_request_resolved",
                "2026-07-27T00:00:03Z",
                {"request_id": "PERM-002", "decision": "approved"},
            )
            with self.assertRaisesRegex(
                ExperimentContractError,
                "experiment_run_id",
            ):
                self._record(
                    controller,
                    manifest={
                        **self._manifest(
                            "agentteam_direct",
                            "operator-ledger-mode-bound-direct",
                        ),
                        "experiment_run_id": self._manifest(
                            "single_codex",
                            "operator-ledger-mode-bound",
                        )["experiment_run_id"],
                    },
                    request=request,
                    response=response,
                )

    def test_source_masquerading_is_rejected_by_controller_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            manifest = self._manifest()
            request = {
                "event_type": "corrective_guidance_received",
                "guidance": "pretend this is a permission answer",
            }
            run_dir = self._prepare_operator_target(
                controller,
                manifest,
                request,
            )
            with self.assertRaisesRegex(
                PermissionError,
                "source authority",
            ):
                controller.record_operator_action(
                    protocol=_protocol(),
                    run_manifest=manifest,
                    run_dir=run_dir,
                    request_source="permission_request",
                    request=request,
                    response={
                        "event_type": "permission_request_resolved",
                        "decision": "approved",
                    },
                    requested_at="2026-07-27T00:00:00Z",
                    answered_at="2026-07-27T00:00:01Z",
                )

    def test_status_and_deterministic_system_events_do_not_create_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            for event_type in (
                "status_inspected",
                "controller_tick",
                "worker_completed",
            ):
                ignored = record_experiment_operator_event(
                    controller,
                    protocol=_protocol(),
                    run_manifest=self._manifest(
                        "agentteam_full",
                        "operator-ledger-status",
                    ),
                    run_dir=Path(tmp) / "unused-status-target",
                    event_type=event_type,
                    request_event={"time": "2026-07-27T00:00:00Z"},
                )
                self.assertEqual(
                    ignored["record_status"],
                    "ignored_non_operator_action",
                )

            projection = controller.operator_action_projection(
                protocol=_protocol(),
                run_manifest=self._manifest(
                    "agentteam_full",
                    "operator-ledger-status",
                ),
            )
            self.assertEqual(projection["action_count"], 0)
            self.assertEqual(projection["intervention_count"], 0)

    def test_corrective_guidance_counts_once_and_bounded_reason_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            request = self._event(
                "corrective_guidance_received",
                "2026-07-27T00:00:00Z",
                {
                    "task_id": "TASK-001",
                    "guidance": "do not retain this free-form instruction",
                },
            )
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-guidance",
            )
            run_dir = self._prepare_operator_target(
                controller,
                manifest,
                request,
            )
            result = record_experiment_operator_event(
                controller,
                protocol=_protocol(),
                run_manifest=manifest,
                run_dir=run_dir,
                event_type="corrective_guidance_received",
                request_event=request,
            )

            entry = result["entry"]
            self.assertTrue(entry["counts_as_intervention"])
            self.assertEqual(result["projection"]["intervention_count"], 1)
            self.assertNotIn(
                "do not retain",
                json.dumps(entry, sort_keys=True),
            )
            self.assertLessEqual(len(entry["reason"]), 240)

    def test_ledger_rejects_authority_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = create_experiment_operator_action_ledger(
                Path(tmp) / "ledger",
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            ledger_path = ledger.root / "operator-actions.jsonl"
            replacement = ledger.root / "replacement.jsonl"
            replacement.write_text("", encoding="utf-8")
            os.replace(replacement, ledger_path)
            with self.assertRaisesRegex(
                ExperimentLedgerIntegrityError,
                "authority changed",
            ):
                load_experiment_operator_action_ledger(ledger.root)

    def test_checkpoint_detects_same_inode_ledger_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = create_experiment_operator_action_ledger(
                Path(tmp) / "ledger",
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-truncation",
            )
            ledger.bind_run(manifest)
            ledger.record(
                experiment_run_id=manifest["experiment_run_id"],
                mode="agentteam_full",
                request_source="permission_request",
                request=self._event(
                    "permission_request_required",
                    "2026-07-27T00:00:00Z",
                    {"request_id": "PERM-001"},
                ),
                response=self._event(
                    "permission_request_resolved",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-001",
                        "decision": "approved",
                    },
                ),
                requested_at="2026-07-27T00:00:00Z",
                answered_at="2026-07-27T00:00:01Z",
            )
            ledger_path = ledger.root / "operator-actions.jsonl"
            ledger_path.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(
                ExperimentLedgerIntegrityError,
                "checkpoint.*ledger",
            ):
                load_experiment_operator_action_ledger(ledger.root)

    def test_experiment_gateways_account_before_applying_runtime_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            output_dir.mkdir()
            events_path = output_dir / "events.jsonl"
            requests = [
                self._event(
                    "manual_gate_required",
                    "2026-07-27T00:00:00Z",
                    {
                        "question_id": "QUESTION-001",
                        "task_id": "TASK-001",
                    },
                ),
                self._event(
                    "permission_request_required",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-001",
                        "task_id": "TASK-001",
                    },
                ),
            ]
            events_path.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in requests
                ),
                encoding="utf-8",
            )
            controller = self._controller(root / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-gateways",
            )
            self._bind_target_run(controller, manifest, output_dir)
            clock = Mock()
            clock.now.return_value = "2026-07-27T00:00:02Z"

            with patch(
                "agentteam_runtime.m0_runtime.answer_manual_gate",
                return_value={
                    "answer_status": "accepted",
                    "question_id": "QUESTION-001",
                },
            ) as answer:
                manual = answer_experiment_manual_gate(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest,
                    output_dir=output_dir,
                    question_id="QUESTION-001",
                    answer="sensitive architecture decision",
                    clock=clock,
                )
            with patch(
                "agentteam_runtime.m0_runtime.resolve_permission_request",
                return_value={
                    "permission_status": "approved",
                    "request_id": "PERM-001",
                },
            ) as permission:
                resolved = resolve_experiment_permission_request(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest,
                    output_dir=output_dir,
                    request_id="PERM-001",
                    decision="approved",
                    reason="sensitive permission reason",
                    clock=clock,
                )

            answer.assert_called_once()
            permission.assert_called_once()
            self.assertEqual(
                manual["operator_action"]["entry"]["action_class"],
                "decision_escalation",
            )
            self.assertEqual(
                resolved["operator_action"]["entry"]["action_class"],
                "expected_operator_action",
            )
            projection = controller.operator_action_projection(
                protocol=_protocol(),
                run_manifest=manifest,
            )
            self.assertEqual(projection["action_count"], 2)
            serialized = json.dumps(projection, sort_keys=True)
            self.assertNotIn("sensitive architecture", serialized)
            self.assertNotIn("sensitive permission", serialized)

    def test_experiment_stop_accounts_before_interrupt_and_runtime_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-stop",
            )
            run_dir = Path(tmp) / "run"
            self._bind_target_run(controller, manifest, run_dir)
            with patch(
                "agentteam_runtime.operator_control.stop_run",
                return_value={"stop_status": "stopped"},
            ) as stop:
                result = stop_experiment_run(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest,
                    run_dir=run_dir,
                    action_request_id="STOP-001",
                    requested_at="2026-07-27T00:00:00Z",
                )

            stop.assert_called_once()
            self.assertEqual(controller.controller_status, "interrupted")
            self.assertEqual(
                result["operator_action"]["entry"]["action_class"],
                "corrective_intervention",
            )

    def test_experiment_stop_capability_validates_against_real_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-stop-capability",
            )
            run_dir = root / "run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "experiment_controller_reference": (
                            controller.reference
                        ),
                        "experiment_run_id": manifest[
                            "experiment_run_id"
                        ],
                        "experiment_run_manifest_sha256": (
                            canonical_json_sha256(manifest)
                        ),
                        "experiment_target_path_sha256": hashlib.sha256(
                            str(run_dir.resolve()).encode("utf-8")
                        ).hexdigest(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = stop_experiment_run(
                controller,
                protocol=_protocol(),
                run_manifest=manifest,
                run_dir=run_dir,
                action_request_id="STOP-CAPABILITY-001",
                requested_at="2026-07-27T00:00:00Z",
            )

            self.assertEqual(result["stop_status"], "stopped")
            self.assertEqual(
                json.loads(
                    (
                        state_dir / "two_phase_scheduler_state.json"
                    ).read_text(encoding="utf-8")
                )["scheduler_status"],
                "stopped",
            )

    def test_gateway_retry_reuses_authoritative_input_after_runtime_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            output_dir.mkdir()
            request = self._event(
                "manual_gate_required",
                "2026-07-27T00:00:00Z",
                {
                    "question_id": "QUESTION-RETRY",
                    "task_id": "TASK-001",
                },
            )
            request["sequence"] = 7
            request["run_id"] = "runtime-run-retry"
            (output_dir / "events.jsonl").write_text(
                json.dumps(request, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            controller = self._controller(root / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-retry",
            )
            self._bind_target_run(controller, manifest, output_dir)
            clock = Mock()
            clock.now.side_effect = [
                "2026-07-27T00:00:01Z",
                "2026-07-27T00:00:02Z",
            ]

            with patch(
                "agentteam_runtime.m0_runtime.answer_manual_gate",
                side_effect=[
                    RuntimeError("runtime failed after accounting"),
                    {
                        "answer_status": "accepted",
                        "question_id": "QUESTION-RETRY",
                    },
                ],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "after accounting",
                ):
                    answer_experiment_manual_gate(
                        controller,
                        protocol=_protocol(),
                        run_manifest=manifest,
                        output_dir=output_dir,
                        question_id="QUESTION-RETRY",
                        answer="stable answer",
                        clock=clock,
                    )
                retried = answer_experiment_manual_gate(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest,
                    output_dir=output_dir,
                    question_id="QUESTION-RETRY",
                    answer="stable answer",
                    clock=clock,
                )

            self.assertEqual(
                retried["operator_action"]["record_status"],
                "already_recorded",
            )
            projection = controller.operator_action_projection(
                protocol=_protocol(),
                run_manifest=manifest,
            )
            self.assertEqual(projection["action_count"], 1)
            source_text = (
                controller.root
                / "operator-actions"
                / "operator-input-events.jsonl"
            ).read_text(encoding="utf-8")
            source_events = [
                json.loads(line)
                for line in source_text.splitlines()
                if line
            ]
            request_source = next(
                event
                for event in source_events
                if event["event_type"] == "manual_gate_required"
            )
            response_source = next(
                event
                for event in source_events
                if event["event_type"] == "operator_answer_received"
            )
            self.assertEqual(
                projection["entries"][0]["request_digest"],
                hashlib.sha256(
                    canonical_json_bytes(request_source)
                ).hexdigest(),
            )
            self.assertEqual(
                projection["entries"][0]["response_digest"],
                hashlib.sha256(
                    canonical_json_bytes(response_source)
                ).hexdigest(),
            )
            self.assertNotIn("stable answer", source_text)
            self.assertIn("payload_sha256", source_text)

    def test_missing_retained_request_authority_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            recorded = self._record(controller)
            source_path = (
                controller.root
                / "operator-actions"
                / "operator-input-events.jsonl"
            )
            source_events = [
                json.loads(line)
                for line in source_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line
            ]
            response_only = [
                event
                for event in source_events
                if event["event_type"] == "permission_request_resolved"
            ]
            source_path.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in response_only
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExperimentLedgerIntegrityError,
                "lacks retained source event authority",
            ):
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=self._manifest(),
                )
            self.assertEqual(recorded["projection"]["action_count"], 1)

    def test_gateway_rejects_manifest_for_another_target_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root / "controller")
            manifest_a = self._manifest(
                "single_codex",
                "operator-ledger-target-a",
            )
            manifest_b = self._manifest(
                "agentteam_full",
                "operator-ledger-target-b",
            )
            run_b = root / "run-b"
            run_b.mkdir()
            self._bind_target_run(controller, manifest_b, run_b)
            (run_b / "events.jsonl").write_text(
                json.dumps(
                    self._event(
                        "manual_gate_required",
                        "2026-07-27T00:00:00Z",
                        {
                            "question_id": "QUESTION-WRONG-RUN",
                            "task_id": "TASK-001",
                        },
                    ),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PermissionError,
                "target run binding",
            ):
                answer_experiment_manual_gate(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest_a,
                    output_dir=run_b,
                    question_id="QUESTION-WRONG-RUN",
                    answer="must not mutate run b",
                )
            self.assertEqual(
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=manifest_b,
                )["action_count"],
                0,
            )

    def test_normalizer_cannot_bypass_target_run_source_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root / "controller")
            manifest_a = self._manifest(
                "single_codex",
                "operator-ledger-authority-a",
            )
            manifest_b = self._manifest(
                "single_codex",
                "operator-ledger-authority-b",
            )
            request = self._event(
                "manual_gate_required",
                "2026-07-27T00:00:00Z",
                {"question_id": "QUESTION-AUTHORITY"},
            )
            response = self._event(
                "operator_answer_received",
                "2026-07-27T00:00:01Z",
                {
                    "question_id": "QUESTION-AUTHORITY",
                    "answer": "approved",
                },
            )
            unbound = root / "unbound"
            unbound.mkdir()

            with self.assertRaisesRegex(
                PermissionError,
                "target run binding",
            ):
                record_experiment_operator_event(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest_b,
                    run_dir=unbound,
                    event_type="operator_answer_received",
                    request_event=request,
                    response_event=response,
                )

            run_a = root / "run-a"
            run_a.mkdir()
            self._bind_target_run(controller, manifest_a, run_a)
            (run_a / "events.jsonl").write_text(
                json.dumps(request, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "target run binding",
            ):
                record_experiment_operator_event(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest_b,
                    run_dir=run_a,
                    event_type="operator_answer_received",
                    request_event=request,
                    response_event=response,
                )

            run_b = root / "run-b-authority"
            run_b.mkdir()
            self._bind_target_run(controller, manifest_b, run_b)
            (run_b / "events.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(
                PermissionError,
                "source authority",
            ):
                record_experiment_operator_event(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest_b,
                    run_dir=run_b,
                    event_type="operator_answer_received",
                    request_event=request,
                    response_event=response,
                )

            self.assertEqual(
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=manifest_b,
                )["action_count"],
                0,
            )

    def test_resume_replay_is_idempotent_after_first_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-resume-retry",
            )
            run_dir = root / "run"
            self._bind_target_run(controller, manifest, run_dir)
            controller.interrupt()

            first = resume_experiment_run(
                controller,
                protocol=_protocol(),
                run_manifest=manifest,
                run_dir=run_dir,
                action_request_id="RESUME-001",
                requested_at="2026-07-27T00:00:00Z",
            )
            replayed = resume_experiment_run(
                controller,
                protocol=_protocol(),
                run_manifest=manifest,
                run_dir=run_dir,
                action_request_id="RESUME-001",
                requested_at="2026-07-27T00:00:01Z",
            )

            self.assertEqual(first["controller_status"], "active")
            self.assertEqual(
                replayed["resume_status"],
                "already_resumed",
            )
            self.assertEqual(
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=manifest,
                )["action_count"],
                1,
            )

    def test_checkpoint_failure_recovers_from_valid_ledger_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = create_experiment_operator_action_ledger(
                Path(tmp) / "ledger",
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-checkpoint-crash",
            )
            ledger.bind_run(manifest)
            request = self._event(
                "permission_request_required",
                "2026-07-27T00:00:00Z",
                {"request_id": "PERM-CRASH"},
            )
            response = self._event(
                "permission_request_resolved",
                "2026-07-27T00:00:01Z",
                {
                    "request_id": "PERM-CRASH",
                    "decision": "approved",
                },
            )
            with patch.object(
                experiment_ledger_module,
                "_append_checkpoint",
                side_effect=OSError("checkpoint write failed"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "checkpoint write failed",
                ):
                    ledger.record(
                        experiment_run_id=manifest["experiment_run_id"],
                        mode="agentteam_full",
                        request_source="permission_request",
                        request=request,
                        response=response,
                        requested_at="2026-07-27T00:00:00Z",
                        answered_at="2026-07-27T00:00:01Z",
                    )

            recovered = load_experiment_operator_action_ledger(ledger.root)
            projection = recovered.replay(
                experiment_run_id=manifest["experiment_run_id"]
            )
            self.assertEqual(projection["action_count"], 1)

    def test_torn_checkpoint_tail_is_discarded_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = create_experiment_operator_action_ledger(
                Path(tmp) / "ledger",
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-torn-checkpoint",
            )
            ledger.bind_run(manifest)
            ledger.record(
                experiment_run_id=manifest["experiment_run_id"],
                mode="agentteam_full",
                request_source="permission_request",
                request=self._event(
                    "permission_request_required",
                    "2026-07-27T00:00:00Z",
                    {"request_id": "PERM-TORN"},
                ),
                response=self._event(
                    "permission_request_resolved",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-TORN",
                        "decision": "approved",
                    },
                ),
                requested_at="2026-07-27T00:00:00Z",
                answered_at="2026-07-27T00:00:01Z",
            )
            checkpoint_path = (
                ledger.root / "operator-action-checkpoints.jsonl"
            )
            with checkpoint_path.open("ab") as stream:
                stream.write(b'{"torn":')

            recovered = load_experiment_operator_action_ledger(ledger.root)
            self.assertEqual(
                recovered.replay(
                    experiment_run_id=manifest["experiment_run_id"]
                )["action_count"],
                1,
            )
            self.assertTrue(
                checkpoint_path.read_bytes().endswith(b"\n")
            )

    def test_first_torn_append_recovers_to_empty_authority_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = create_experiment_operator_action_ledger(
                Path(tmp) / "ledger",
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-first-torn",
            )
            bindings_path = (
                ledger.root / "operator-run-bindings.jsonl"
            )
            bindings_path.write_bytes(b'{"partial":')
            ledger.bind_run(manifest)

            source_path = ledger.root / "operator-input-events.jsonl"
            source_path.write_bytes(b'{"partial":')
            ledger_path = ledger.root / "operator-actions.jsonl"
            ledger_path.write_bytes(b'{"partial":')
            recorded = ledger.record(
                experiment_run_id=manifest["experiment_run_id"],
                mode="agentteam_full",
                request_source="permission_request",
                request=self._event(
                    "permission_request_required",
                    "2026-07-27T00:00:00Z",
                    {"request_id": "PERM-FIRST-TORN"},
                ),
                response=self._event(
                    "permission_request_resolved",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-FIRST-TORN",
                        "decision": "approved",
                    },
                ),
                requested_at="2026-07-27T00:00:00Z",
                answered_at="2026-07-27T00:00:01Z",
            )
            self.assertEqual(recorded["record_status"], "recorded")

            checkpoint_path = (
                ledger.root / "operator-action-checkpoints.jsonl"
            )
            checkpoint_path.write_bytes(b'{"partial":')
            recovered = load_experiment_operator_action_ledger(ledger.root)
            self.assertEqual(
                recovered.replay(
                    experiment_run_id=manifest["experiment_run_id"]
                )["action_count"],
                1,
            )

    def test_stop_retry_reuses_action_and_already_interrupted_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-stop-retry",
            )
            run_dir = Path(tmp) / "run"
            self._bind_target_run(controller, manifest, run_dir)
            with patch(
                "agentteam_runtime.operator_control.stop_run",
                side_effect=[
                    RuntimeError("stop failed after interrupt"),
                    {"stop_status": "stopped"},
                ],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "after interrupt",
                ):
                    stop_experiment_run(
                        controller,
                        protocol=_protocol(),
                        run_manifest=manifest,
                        run_dir=run_dir,
                        action_request_id="STOP-RETRY-001",
                        requested_at="2026-07-27T00:00:00Z",
                    )
                retried = stop_experiment_run(
                    controller,
                    protocol=_protocol(),
                    run_manifest=manifest,
                    run_dir=run_dir,
                    action_request_id="STOP-RETRY-001",
                    requested_at="2026-07-27T00:00:01Z",
                )

            self.assertEqual(
                retried["operator_action"]["record_status"],
                "already_recorded",
            )
            self.assertEqual(controller.controller_status, "interrupted")
            self.assertEqual(
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=manifest,
                )["action_count"],
                1,
            )

    def test_terminal_rerun_can_bind_a_new_stable_request_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            first = self._record(
                controller,
                manifest=self._manifest(
                    "single_codex",
                    "operator-ledger-original-key",
                ),
            )
            second = self._record(
                controller,
                manifest=self._manifest(
                    "single_codex",
                    "operator-ledger-alternate-key",
                ),
            )

            self.assertNotEqual(
                first["entry"]["experiment_run_id"],
                second["entry"]["experiment_run_id"],
            )
            self.assertEqual(first["projection"]["action_count"], 1)
            self.assertEqual(second["projection"]["action_count"], 1)

    def test_request_response_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(Path(tmp) / "controller")
            with self.assertRaisesRegex(
                ExperimentLedgerError,
                "identities do not match",
            ):
                self._record(
                    controller,
                    request=self._event(
                        "permission_request_required",
                        "2026-07-27T00:00:00Z",
                        {"request_id": "PERM-REAL"},
                    ),
                    response=self._event(
                        "permission_request_resolved",
                        "2026-07-27T00:00:01Z",
                        {
                            "request_id": "PERM-OTHER",
                            "decision": "approved",
                        },
                    ),
                )

    def test_legacy_budget_controller_loads_without_operator_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "controller"
            create_experiment_controller(
                root,
                protocol_id="legacy-budget-only",
                max_total_tokens=100,
                max_wall_time_seconds=3600,
                soft_warning_ratio=0.8,
                scored=True,
            )
            metadata_path = root / "experiment-budget-controller.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["schema_version"] = "experiment_budget_controller.v1"
            metadata.pop("protocol_sha256")
            metadata.pop("operator_limits")
            metadata.pop("operator_limits_sha256")
            metadata.pop("operator_ledger_root_device")
            metadata.pop("operator_ledger_root_inode")
            metadata.pop("operator_ledger_policy_sha256")
            metadata_path.write_text(
                json.dumps(metadata, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            legacy = create_experiment_controller(
                root,
                protocol_id="legacy-budget-only",
                max_total_tokens=100,
                max_wall_time_seconds=3600,
                soft_warning_ratio=0.8,
                scored=True,
            )
            self.assertEqual(legacy.controller_status, "active")
            legacy_protocol = {
                **_protocol(),
                "experiment_id": "legacy-budget-only",
            }
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "lacks frozen operator policy",
            ):
                legacy.operator_action_projection(
                    protocol=legacy_protocol,
                    run_manifest=build_experiment_run_manifest(
                        legacy_protocol,
                        mode="single_codex",
                        repetition_index=0,
                        stable_request_key="legacy-budget-only",
                    ),
                )

    def test_mixed_v1_metadata_with_v2_fields_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "controller"
            self._controller(root)
            metadata_path = root / "experiment-budget-controller.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["schema_version"] = "experiment_budget_controller.v1"
            metadata_path.write_text(
                json.dumps(metadata, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "cannot contain v2 operator fields",
            ):
                self._controller(root)

    def test_ordinary_stop_entrypoint_rejects_experiment_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            state = {
                "experiment_controller_reference": {
                    "schema_version": (
                        "experiment_budget_controller_reference.v1"
                    )
                },
                "backlog": {"items": [{"task_id": "TASK-001"}]},
            }
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            events = [
                self._event(
                    "manual_gate_required",
                    "2026-07-27T00:00:00Z",
                    {
                        "question_id": "QUESTION-GUARD",
                        "task_id": "TASK-001",
                    },
                ),
                self._event(
                    "permission_request_required",
                    "2026-07-27T00:00:01Z",
                    {
                        "request_id": "PERM-GUARD",
                        "task_id": "TASK-001",
                    },
                ),
            ]
            (run_dir / "events.jsonl").write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in events
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PermissionError,
                "bound ledger authorization",
            ):
                stop_run(run_dir)

    def test_generic_stale_cleanup_skips_experiment_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / "runs" / "experiment-run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            state_path = state_dir / "two_phase_scheduler_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "experiment_controller_reference": {
                            "schema_version": (
                                "experiment_budget_controller_reference.v1"
                            )
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            summary = cleanup_stale_runs(
                {"work_root": str(work_root)}
            )

            self.assertEqual(
                summary["runs"][0]["stop_status"],
                "experiment_gateway_required",
            )
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "scheduler_status"
                ],
                "running",
            )

    def test_forged_boolean_cannot_authorize_experiment_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "experiment_controller_reference": {
                            "schema_version": (
                                "experiment_budget_controller_reference.v1"
                            )
                        },
                        "backlog": {
                            "items": [{"task_id": "TASK-001"}]
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "bound ledger authorization",
            ):
                stop_run(
                    run_dir,
                    experiment_operator_authorization=True,
                )

    def test_controller_rejects_deleted_operator_ledger_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "controller"
            controller = self._controller(root)
            manifest = self._manifest(
                "agentteam_full",
                "operator-ledger-delete",
            )
            self._record(controller, manifest=manifest)
            shutil.rmtree(root / "operator-actions")

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "ledger authority is unavailable",
            ):
                controller.operator_action_projection(
                    protocol=_protocol(),
                    run_manifest=manifest,
                )
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "ledger authority is unavailable",
            ):
                self._controller(root)

    def test_policy_short_write_is_completed_and_failed_publish_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "short-write"
            original_write = os.write

            def partial_write(fd, payload):
                return original_write(fd, payload[: min(11, len(payload))])

            with patch.object(
                experiment_ledger_module.os,
                "write",
                side_effect=partial_write,
            ):
                ledger = create_experiment_operator_action_ledger(
                    root,
                    protocol_id="phase2-operator-ledger",
                    protocol_sha256=canonical_json_sha256(_protocol()),
                    operator_limits=self.LIMITS,
                )
            self.assertEqual(
                ledger.policy["protocol_sha256"],
                canonical_json_sha256(_protocol()),
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "failed-write"
            with patch.object(
                experiment_ledger_module.os,
                "write",
                return_value=0,
            ):
                with self.assertRaisesRegex(
                    ExperimentLedgerIntegrityError,
                    "write was incomplete",
                ):
                    create_experiment_operator_action_ledger(
                        root,
                        protocol_id="phase2-operator-ledger",
                        protocol_sha256=canonical_json_sha256(
                            _protocol()
                        ),
                        operator_limits=self.LIMITS,
                    )
            self.assertFalse(
                (root / "operator-action-policy.json").exists()
            )
            recovered = create_experiment_operator_action_ledger(
                root,
                protocol_id="phase2-operator-ledger",
                protocol_sha256=canonical_json_sha256(_protocol()),
                operator_limits=self.LIMITS,
            )
            self.assertEqual(
                recovered.policy["protocol_id"],
                "phase2-operator-ledger",
            )


class ExperimentResultBundleTests(unittest.TestCase):
    @staticmethod
    def _fixture(root, *, terminal_status="completed", run_parent=None):
        root = Path(root)
        release = _release()
        allocation_root = (
            Path(run_parent).parent if run_parent is not None else root
        )
        allocation = allocate_experiment_run(
            allocation_root,
            _protocol(),
            mode="agentteam_full",
            repetition_index=0,
            stable_request_key=f"result-{terminal_status}",
            runtime_release=release,
            bound_at="2026-07-27T00:00:00Z",
        )
        manifest = allocation["run_manifest"]
        run_dir = Path(allocation["run_dir"])
        retained = {}
        for group in ("prompt", "context", "taskpack", "artifacts"):
            path = run_dir / group
            path.mkdir()
            (path / f"{group}.txt").write_text(
                f"{group} authority\n",
                encoding="utf-8",
            )
            retained[group] = [str(path)]
        spool = run_dir / "raw-spool"
        spool.mkdir()
        (spool / "provider.jsonl").write_text(
            "raw provider output\n",
            encoding="utf-8",
        )
        (run_dir / "artifacts" / "agentteam.db").write_bytes(b"db")
        authority_root = root / f"authority-{run_dir.name}"
        authority_root.mkdir(mode=0o700)
        controller = create_experiment_controller(
            authority_root / "controller",
            protocol_id=_protocol()["experiment_id"],
            max_total_tokens=_protocol()["budgets"][
                "max_total_tokens"
            ],
            max_wall_time_seconds=_protocol()["budgets"][
                "max_wall_time_seconds"
            ],
            soft_warning_ratio=_protocol()["budgets"][
                "soft_warning_ratio"
            ],
            scored=_protocol()["scored"],
            protocol_sha256=canonical_json_sha256(_protocol()),
            operator_limits=_protocol()["operator_limits"],
        )
        controller.interrupt()
        controller_snapshot = controller.snapshot()
        snapshot_path = run_dir / "repository"
        snapshot_path.mkdir()
        snapshot_common_dir = snapshot_path / ".git-common"
        snapshot_common_dir.mkdir()
        source_common_dir = root / f"source-common-{run_dir.name}"
        source_common_dir.mkdir()
        clean_snapshot_path = run_dir / "clean-snapshot.json"
        clean_snapshot = {
            "schema_version": "experiment_clean_snapshot.v1",
            "experiment_run_id": run_dir.name,
            "attested_at": "2026-07-27T00:00:15Z",
            "repository": copy.deepcopy(_protocol()["repository"]),
            "snapshot_path": str(snapshot_path),
            "snapshot_common_dir": str(snapshot_common_dir),
            "source_common_dir": str(source_common_dir),
            "path_preexisted": False,
            "head_commit": _protocol()["repository"]["commit"],
            "head_tree": _protocol()["repository"]["tree"],
            "git_object_format": "sha1",
            "detached_head": True,
            "worktree_clean": True,
            "common_dirs_distinct": True,
            "remotes": [],
            "alternates": [],
            "extra_refs": [],
            "file_inventory": {
                "entry_count": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "inventory_limit": 100,
                "matches_source": True,
            },
            "symlink_escape_count": 0,
            "tracked_files_only": True,
            "prior_run_state_detected": False,
        }
        clean_snapshot_path.write_bytes(
            canonical_json_bytes(clean_snapshot) + b"\n"
        )
        clean_snapshot_reference = {
            "schema_version": "clean_snapshot_attestation.v1",
            "path": str(clean_snapshot_path),
            "sha256": hashlib.sha256(
                clean_snapshot_path.read_bytes()
            ).hexdigest(),
        }
        scan_reference = publish_result_scan_scope(
            authority_root,
            retained,
        )
        from agentteam_runtime.experiment_sandbox import (
            load_scan_scope_reference,
        )

        _scope, scan_digest = load_scan_scope_reference(
            scan_reference,
            authority_root,
        )
        evaluation_path = run_dir / "artifacts" / "evaluation.json"
        evaluation = {
            "scan_scope_sha256": scan_digest,
            "evaluation_status": (
                "passed" if terminal_status == "completed" else "failed"
            ),
        }
        evaluation_path.write_text(
            json.dumps(evaluation, sort_keys=True),
            encoding="utf-8",
        )
        metrics = measure_experiment_artifacts(
            run_dir,
            artifact_roots=retained["artifacts"],
            raw_spool_roots=[spool],
        )
        evaluation_sha256 = hashlib.sha256(
            evaluation_path.read_bytes()
        ).hexdigest()
        protocol_reference = {
            "schema_version": "experiment_protocol_reference.v1",
            "path": str(authority_root / "protocol.json"),
            "sha256": "8" * 64,
        }
        invocation_reference = {
            "schema_version": "experiment_invocation_set_reference.v1",
            "path": str(authority_root / "invocations.json"),
            "sha256": "9" * 64,
        }
        sandbox_reference = {
            "schema_version": "provider_sandbox_reference.v1",
            "path": str(authority_root / "sandbox.json"),
            "sha256": "a" * 64,
        }
        evidence = {
            "evaluation_relative_path": "artifacts/evaluation.json",
            "evaluation_sha256": evaluation_sha256,
            "taskpack_ids": ["TASKPACK-001"],
            "protocol_reference_sha256": protocol_reference["sha256"],
            "invocation_set_reference_sha256": (
                invocation_reference["sha256"]
            ),
            "scan_scope_reference_sha256": scan_reference["sha256"],
            "scan_scope_sha256": scan_digest,
            "acceptance_command_sha256": canonical_json_sha256(
                _protocol()["acceptance"]["command"]
            ),
            "acceptance_executable_sha256": "b" * 64,
            "evaluator_sha256": _protocol()["evaluator"][
                "artifact_sha256"
            ],
            "provider_sandbox_reference_sha256": (
                sandbox_reference["sha256"]
            ),
        }
        bundle = build_experiment_result_bundle(
            protocol=_protocol(),
            run_manifest=manifest,
            runtime_release_identity=release,
            started_at="2026-07-27T00:00:00Z",
            finished_at="2026-07-27T00:01:00Z",
            terminal_status=terminal_status,
            acceptance_result={
                "status": (
                    "passed"
                    if terminal_status == "completed"
                    else "failed"
                ),
                "evaluation_sha256": evaluation_sha256,
            },
            usage_totals={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
            usage_coverage={
                "status": "complete",
                "covered_invocations": 1,
                "total_invocations": 1,
            },
            budget_result={"status": "within_budget"},
            attempt_counts={"total": 1, "accepted": 1},
            verified_milestones=["fixture"],
            operator_action_counts={
                "expected_operator_action": 0,
                "corrective_intervention": 0,
                "decision_escalation": 0,
            },
            retry_and_repair_counts={"retries": 0, "repairs": 0},
            changed_files=["src/fixture.py"],
            regressions=[],
            artifact_bytes_written=metrics["artifact_bytes_written"],
            raw_spool_bytes_written=metrics[
                "raw_spool_bytes_written"
            ],
            workspace_diff_sha256="c" * 64,
            result_evidence=evidence,
            cleanup_status="pending",
        )
        return {
            "run_dir": run_dir,
            "retained": retained,
            "spool": spool,
            "authority_root": authority_root,
            "scan_reference": scan_reference,
            "evaluation_path": evaluation_path,
            "manifest": manifest,
            "release": release,
            "controller": controller,
            "controller_snapshot": controller_snapshot,
            "controller_reference": controller.reference,
            "clean_snapshot_reference": clean_snapshot_reference,
            "resume_binding_sha256": canonical_json_sha256(
                allocation["binding"]
            ),
            "protocol_reference": protocol_reference,
            "invocation_reference": invocation_reference,
            "sandbox_reference": sandbox_reference,
            "bundle": bundle,
            "metrics": metrics,
        }

    def _seal(self, fixture, bundle=None):
        with patch(
            "agentteam_runtime.experiment_results."
            "validate_evaluation_evidence"
        ) as validate:
            result = seal_experiment_result_bundle(
                fixture["run_dir"],
                bundle or fixture["bundle"],
                protocol=_protocol(),
                run_manifest=fixture["manifest"],
                runtime_release_identity=fixture["release"],
                authority_root=fixture["authority_root"],
                evaluation_path=fixture["evaluation_path"],
                invocation_set_reference=fixture[
                    "invocation_reference"
                ],
                protocol_reference=fixture["protocol_reference"],
                provider_sandbox_reference=fixture[
                    "sandbox_reference"
                ],
                scan_scope_reference=fixture["scan_reference"],
                retained_roots=fixture["retained"],
                canary_path=fixture["evaluation_path"],
                raw_spool_roots=[fixture["spool"]],
            )
        return result, validate

    def test_terminal_bundle_is_atomic_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            first, validate = self._seal(fixture)
            replayed, _replay_validate = self._seal(fixture)

            self.assertEqual(
                first["bundle_sha256"],
                replayed["bundle_sha256"],
            )
            self.assertEqual(
                load_experiment_result_bundle(
                    fixture["run_dir"]
                )["bundle"],
                fixture["bundle"],
            )
            validate.assert_called_once()
            changed = copy.deepcopy(fixture["bundle"])
            changed["changed_files"].append("src/other.py")
            with self.assertRaisesRegex(
                ExperimentResultConflict,
                "conflicts",
            ):
                self._seal(fixture, changed)
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "terminal experiment",
            ):
                write_experiment_recovery_snapshot(
                    fixture["run_dir"],
                    protocol=_protocol(),
                    run_manifest=fixture["manifest"],
                    runtime_release_identity=fixture["release"],
                    snapshot_sequence=1,
                    captured_at="2026-07-27T00:02:00Z",
                    controller_snapshot={
                        "controller_status": "interrupted",
                        "budget_state": {"status": "within_budget"},
                        "checkpoint_sequence": 1,
                    },
                    operator_action_counts={
                        "expected_operator_action": 0,
                        "corrective_intervention": 1,
                        "decision_escalation": 0,
                    },
                    resume_binding_sha256="d" * 64,
                    recovery_context={},
                )

    def test_bundle_rejects_subset_scope_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            subset = copy.deepcopy(fixture["retained"])
            extra = fixture["run_dir"] / "extra-artifacts"
            extra.mkdir()
            subset["artifacts"] = [str(extra)]
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "complete retained root set",
            ):
                with patch(
                    "agentteam_runtime.experiment_results."
                    "validate_evaluation_evidence"
                ):
                    seal_experiment_result_bundle(
                        fixture["run_dir"],
                        fixture["bundle"],
                        protocol=_protocol(),
                        run_manifest=fixture["manifest"],
                        runtime_release_identity=fixture["release"],
                        authority_root=fixture["authority_root"],
                        evaluation_path=fixture["evaluation_path"],
                        invocation_set_reference=fixture[
                            "invocation_reference"
                        ],
                        protocol_reference=fixture[
                            "protocol_reference"
                        ],
                        provider_sandbox_reference=fixture[
                            "sandbox_reference"
                        ],
                        scan_scope_reference=fixture["scan_reference"],
                        retained_roots=subset,
                        canary_path=fixture["evaluation_path"],
                        raw_spool_roots=[fixture["spool"]],
                    )

            wrong = copy.deepcopy(fixture["bundle"])
            wrong["experiment_run_id"] = "RUN-WRONG"
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "frozen run authority",
            ):
                self._seal(fixture, wrong)

    def test_artifact_bytes_exclude_db_and_raw_spool(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            metrics = fixture["metrics"]

            self.assertGreater(metrics["artifact_bytes_written"], 0)
            self.assertEqual(
                metrics["raw_spool_bytes_written"],
                len(b"raw provider output\n"),
            )
            self.assertEqual(metrics["raw_spool_file_count"], 1)
            nested_spool = (
                fixture["run_dir"] / "artifacts" / "nested-raw-spool"
            )
            nested_spool.mkdir()
            nested_payload = b"nested raw output\n"
            (nested_spool / "provider.jsonl").write_bytes(nested_payload)
            cache = fixture["run_dir"] / "artifacts" / ".cache"
            cache.mkdir()
            for index in range(3):
                (cache / str(index)).write_text("ignored", encoding="utf-8")
            with patch(
                "agentteam_runtime.experiment_results."
                "_MAX_ARTIFACT_FILES",
                3,
            ):
                measured = measure_experiment_artifacts(
                    fixture["run_dir"],
                    artifact_roots=fixture["retained"]["artifacts"],
                    raw_spool_roots=[
                        fixture["spool"],
                        nested_spool,
                    ],
                )
            self.assertEqual(
                measured["raw_spool_bytes_written"],
                len(b"raw provider output\n") + len(nested_payload),
            )
            self.assertEqual(measured["raw_spool_file_count"], 2)

    def test_atomic_publication_does_not_replace_racing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            import agentteam_runtime.experiment_results as results_module

            original = results_module._rename_directory_noreplace

            def race(source, destination):
                Path(destination).mkdir()
                return original(source, destination)

            with patch.object(
                results_module,
                "_rename_directory_noreplace",
                side_effect=race,
            ):
                with self.assertRaisesRegex(
                    ExperimentResultIntegrityError,
                    "directory fields are invalid",
                ):
                    self._seal(fixture)
            self.assertEqual(
                list(
                    (fixture["run_dir"] / "results" / "terminal").iterdir()
                ),
                [],
            )

    def test_recovery_snapshots_are_versioned_and_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            arguments = {
                "protocol": _protocol(),
                "run_manifest": fixture["manifest"],
                "runtime_release_identity": fixture["release"],
                "snapshot_sequence": 1,
                "captured_at": "2026-07-27T00:00:30Z",
                "controller_snapshot": fixture["controller_snapshot"],
                "operator_action_counts": {
                    "expected_operator_action": 0,
                    "corrective_intervention": 1,
                    "decision_escalation": 0,
                },
                "resume_binding_sha256": fixture[
                    "resume_binding_sha256"
                ],
                "recovery_context": {
                    "resume_phase": "running",
                    "reason": "operator pause",
                    "run_state_version": 4,
                    "controller_reference": fixture[
                        "controller_reference"
                    ],
                    "clean_snapshot_attestation": fixture[
                        "clean_snapshot_reference"
                    ],
                    "scan_scope_reference": fixture["scan_reference"],
                    "invocation_set_reference": fixture[
                        "invocation_reference"
                    ],
                    "open_invocation_ids": ["INV-FIXTURE-001"],
                    "integration_active": False,
                    "adapter_checkpoint": None,
                },
            }
            first = write_experiment_recovery_snapshot(
                fixture["run_dir"],
                **arguments,
            )
            replayed = write_experiment_recovery_snapshot(
                fixture["run_dir"],
                **arguments,
            )

            self.assertEqual(
                first["snapshot_sha256"],
                replayed["snapshot_sha256"],
            )
            latest = load_latest_experiment_recovery_snapshot(
                fixture["run_dir"]
            )
            self.assertTrue(latest["snapshot"]["resumable"])
            self.assertFalse(
                (fixture["run_dir"] / "results" / "terminal").exists()
            )
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "sequence is not contiguous",
            ):
                write_experiment_recovery_snapshot(
                    fixture["run_dir"],
                    **{
                        **arguments,
                        "snapshot_sequence": 3,
                    },
                )
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "resume binding",
            ):
                write_experiment_recovery_snapshot(
                    fixture["run_dir"],
                    **{
                        **arguments,
                        "snapshot_sequence": 2,
                        "resume_binding_sha256": "e" * 64,
                    },
                )
            tampered_context = copy.deepcopy(
                arguments["recovery_context"]
            )
            tampered_context["clean_snapshot_attestation"][
                "sha256"
            ] = "f" * 64
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "attestation digest",
            ):
                write_experiment_recovery_snapshot(
                    fixture["run_dir"],
                    **{
                        **arguments,
                        "snapshot_sequence": 2,
                        "recovery_context": tampered_context,
                    },
                )
            unrelated_protocol = copy.deepcopy(_protocol())
            unrelated_protocol["experiment_id"] = "unrelated-protocol"
            unrelated = create_experiment_controller(
                fixture["authority_root"] / "unrelated-controller",
                protocol_id=unrelated_protocol["experiment_id"],
                budget_id="unrelated-budget",
                max_total_tokens=unrelated_protocol["budgets"][
                    "max_total_tokens"
                ],
                max_wall_time_seconds=unrelated_protocol["budgets"][
                    "max_wall_time_seconds"
                ],
                soft_warning_ratio=unrelated_protocol["budgets"][
                    "soft_warning_ratio"
                ],
                scored=unrelated_protocol["scored"],
                protocol_sha256=canonical_json_sha256(
                    unrelated_protocol
                ),
                operator_limits=unrelated_protocol[
                    "operator_limits"
                ],
            )
            unrelated.interrupt()
            unrelated_context = copy.deepcopy(
                arguments["recovery_context"]
            )
            unrelated_context["controller_reference"] = (
                unrelated.reference
            )
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "run protocol authority",
            ):
                write_experiment_recovery_snapshot(
                    fixture["run_dir"],
                    **{
                        **arguments,
                        "snapshot_sequence": 2,
                        "controller_snapshot": unrelated.snapshot(),
                        "recovery_context": unrelated_context,
                    },
                )
            fixture["controller"].resume_interrupted(
                reference=fixture["controller_reference"]
            )
            with self.assertRaisesRegex(
                ExperimentResultIntegrityError,
                "live controller authority",
            ):
                load_latest_experiment_recovery_snapshot(
                    fixture["run_dir"]
                )

    def test_terminal_and_snapshot_publication_share_run_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            import threading

            import agentteam_runtime.experiment_results as results_module

            snapshot_inside_publication = threading.Event()
            release_snapshot = threading.Event()
            terminal_finished = threading.Event()
            failures = []
            original_publish = results_module._publish_sealed_directory

            def delayed_publish(*args, **kwargs):
                final_dir = Path(args[0])
                if final_dir.parent.name == "recovery":
                    snapshot_inside_publication.set()
                    if not release_snapshot.wait(5):
                        raise AssertionError(
                            "snapshot publication was not released"
                        )
                return original_publish(*args, **kwargs)

            recovery_context = {
                "resume_phase": "running",
                "reason": "publication race regression",
                "run_state_version": 4,
                "controller_reference": fixture["controller_reference"],
                "clean_snapshot_attestation": fixture[
                    "clean_snapshot_reference"
                ],
                "scan_scope_reference": fixture["scan_reference"],
                "invocation_set_reference": fixture[
                    "invocation_reference"
                ],
                "open_invocation_ids": ["INV-FIXTURE-001"],
                "integration_active": False,
                "adapter_checkpoint": None,
            }

            def publish_snapshot():
                try:
                    write_experiment_recovery_snapshot(
                        fixture["run_dir"],
                        protocol=_protocol(),
                        run_manifest=fixture["manifest"],
                        runtime_release_identity=fixture["release"],
                        snapshot_sequence=1,
                        captured_at="2026-07-27T00:00:30Z",
                        controller_snapshot=fixture[
                            "controller_snapshot"
                        ],
                        operator_action_counts={
                            "expected_operator_action": 0,
                            "corrective_intervention": 1,
                            "decision_escalation": 0,
                        },
                        resume_binding_sha256=fixture[
                            "resume_binding_sha256"
                        ],
                        recovery_context=recovery_context,
                    )
                except Exception as exc:  # pragma: no cover - assertion aid
                    failures.append(exc)

            def publish_terminal():
                try:
                    self._seal(fixture)
                except Exception as exc:  # pragma: no cover - assertion aid
                    failures.append(exc)
                finally:
                    terminal_finished.set()

            with patch.object(
                results_module,
                "_publish_sealed_directory",
                side_effect=delayed_publish,
            ):
                snapshot_thread = threading.Thread(
                    target=publish_snapshot,
                    daemon=True,
                )
                snapshot_thread.start()
                self.assertTrue(snapshot_inside_publication.wait(5))
                terminal_thread = threading.Thread(
                    target=publish_terminal,
                    daemon=True,
                )
                terminal_thread.start()
                self.assertFalse(terminal_finished.wait(0.2))
                release_snapshot.set()
                snapshot_thread.join(5)
                terminal_thread.join(5)

            self.assertFalse(snapshot_thread.is_alive())
            self.assertFalse(terminal_thread.is_alive())
            self.assertEqual(failures, [])
            latest = load_latest_experiment_recovery_snapshot(
                fixture["run_dir"]
            )
            self.assertFalse(latest["resumable"])
            self.assertTrue(latest["superseded_by_terminal"])

    def test_projection_rebuild_preserves_all_outcomes_and_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            (work_root / "runs").mkdir(parents=True)
            sealed = []
            for status in ("completed", "failed", "interrupted"):
                fixture = self._fixture(
                    work_root,
                    terminal_status=status,
                    run_parent=work_root / "runs",
                )
                if status == "interrupted":
                    write_experiment_recovery_snapshot(
                        fixture["run_dir"],
                        protocol=_protocol(),
                        run_manifest=fixture["manifest"],
                        runtime_release_identity=fixture["release"],
                        snapshot_sequence=1,
                        captured_at="2026-07-27T00:00:30Z",
                        controller_snapshot=fixture[
                            "controller_snapshot"
                        ],
                        operator_action_counts={
                            "expected_operator_action": 0,
                            "corrective_intervention": 1,
                            "decision_escalation": 0,
                        },
                        resume_binding_sha256=fixture[
                            "resume_binding_sha256"
                        ],
                        recovery_context={
                            "resume_phase": "running",
                            "reason": "projection fixture",
                            "run_state_version": 2,
                            "controller_reference": fixture[
                                "controller_reference"
                            ],
                            "clean_snapshot_attestation": fixture[
                                "clean_snapshot_reference"
                            ],
                            "scan_scope_reference": fixture[
                                "scan_reference"
                            ],
                            "invocation_set_reference": fixture[
                                "invocation_reference"
                            ],
                            "open_invocation_ids": [],
                            "integration_active": False,
                            "adapter_checkpoint": None,
                        },
                    )
                result, _validate = self._seal(fixture)
                sealed.append(result)

            rebuilt = rebuild_project_projection_db(work_root)
            checked = check_project_projection_db(work_root)
            projected = read_projected_experiment_results(work_root)

            self.assertEqual(rebuilt["experiment_results"], 3)
            self.assertEqual(rebuilt["experiment_recovery"], 1)
            self.assertEqual(checked["check_status"], "passed")
            self.assertEqual(
                {item["bundle"]["terminal_status"] for item in projected},
                {"completed", "failed", "interrupted"},
            )
            self.assertEqual(
                {item["bundle_sha256"] for item in projected},
                {item["bundle_sha256"] for item in sealed},
            )
            recovery = read_projected_experiment_recovery(work_root)
            self.assertEqual(len(recovery), 1)
            self.assertFalse(recovery[0]["resumable"])
            (work_root / "agentteam.db").unlink()
            fallback = read_projected_experiment_results(work_root)
            self.assertEqual(
                {item["bundle"]["terminal_status"] for item in fallback},
                {"completed", "failed", "interrupted"},
            )
            self.assertTrue(
                all(item["projection_source"] == "files" for item in fallback)
            )

    def test_comparison_retains_unsuccessful_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = self._fixture(
                Path(tmp) / "completed"
            )["bundle"]
            failed = self._fixture(
                Path(tmp) / "failed",
                terminal_status="failed",
            )["bundle"]
            interrupted = self._fixture(
                Path(tmp) / "interrupted",
                terminal_status="interrupted",
            )["bundle"]

            comparison = render_experiment_comparison(
                [
                    {"bundle": completed, "bundle_sha256": "1" * 64},
                    {"bundle": failed, "bundle_sha256": "2" * 64},
                    {"bundle": interrupted, "bundle_sha256": "3" * 64},
                ]
            )
            self.assertIn("completed", comparison)
            self.assertIn("failed", comparison)
            self.assertIn("interrupted", comparison)
            self.assertIn(
                "tokens: 120",
                render_experiment_result(completed),
            )


class ExperimentModeAdapterTests(unittest.TestCase):
    @staticmethod
    def _fixture(root, mode, *, direct_taskpack_sha256=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        repository = _fixture_repository(root)
        protocol = copy.deepcopy(_protocol())
        protocol["repository"] = repository["repository"]
        for seed in range(100):
            seeded_order = sorted(
                protocol["modes"],
                key=lambda candidate: canonical_json_sha256(
                    {"seed": seed, "mode": candidate}
                ),
            )
            if seeded_order[0] == mode:
                protocol["seed"] = seed
                protocol["mode_order"] = seeded_order
                break
        else:
            raise AssertionError("no deterministic fixture mode seed")
        evaluator = root / "trusted-mode-evaluator.py"
        evaluator.write_text(
            "#!/usr/bin/python3\nraise SystemExit(0)\n",
            encoding="utf-8",
        )
        evaluator.chmod(0o700)
        protocol["evaluator"]["artifact_sha256"] = hashlib.sha256(
            evaluator.read_bytes()
        ).hexdigest()
        if direct_taskpack_sha256 is not None:
            protocol["direct_taskpack"][
                "sha256"
            ] = direct_taskpack_sha256
        manifest = build_experiment_run_manifest(
            protocol,
            mode=mode,
            repetition_index=0,
            stable_request_key=f"mode-{mode}",
        )
        run_dir = root / manifest["experiment_run_id"]
        run_dir.mkdir()
        snapshot = allocate_clean_snapshot(
            run_dir,
            repository["repository"],
            attested_at="2026-07-27T00:00:00Z",
        )
        authority_root = run_dir / "authority"
        authority_root.mkdir(mode=0o700)
        controller = create_experiment_controller(
            root / "protocol-controller",
            protocol_id=protocol["experiment_id"],
            max_total_tokens=protocol["budgets"][
                "max_total_tokens"
            ],
            max_wall_time_seconds=protocol["budgets"][
                "max_wall_time_seconds"
            ],
            soft_warning_ratio=protocol["budgets"][
                "soft_warning_ratio"
            ],
            scored=protocol["scored"],
            protocol_sha256=canonical_json_sha256(protocol),
            operator_limits=protocol["operator_limits"],
        )
        credential = root / "provider-credential.json"
        credential.write_text("{}\n", encoding="utf-8")
        canary = root / "gold-canary"
        canary.write_text("mode-only-canary\n", encoding="utf-8")
        sandbox_configuration = {
            "runtime_views": [
                {"source": path, "target": path}
                for path in ("/usr", "/lib", "/lib64", "/bin")
                if Path(path).exists()
            ],
            "library_views": [],
            "credential_mounts": [
                {
                    "source": str(credential),
                    "target": (
                        "/run/agentteam-credentials/provider.json"
                    ),
                }
            ],
            "environment": {
                "AGENTTEAM_CREDENTIAL_FILE": (
                    "/run/agentteam-credentials/provider.json"
                )
            },
            "canary_path": str(canary),
        }
        return {
            "protocol": protocol,
            "manifest": manifest,
            "run_dir": run_dir,
            "snapshot": Path(snapshot["snapshot_path"]),
            "authority_root": authority_root,
            "controller": controller,
            "sandbox_configuration": sandbox_configuration,
            "evaluator": evaluator,
        }

    @staticmethod
    def _controller(fixture):
        return ExperimentModeController(
            protocol=fixture["protocol"],
            run_manifest=fixture["manifest"],
            run_dir=fixture["run_dir"],
            project_root=fixture["snapshot"],
            authority_root=fixture["authority_root"],
            controller_reference=fixture["controller"].reference,
            sandbox_configuration=fixture["sandbox_configuration"],
            common_finalizer=ExperimentCommonFinalizer(
                evaluator_artifact=fixture["evaluator"],
                runtime_release_identity=_release(),
            ),
            runtime_release_identity=_release(),
        )

    @staticmethod
    @contextmanager
    def _mode_execution_boundary():
        def fake_evaluator(**kwargs):
            from agentteam_runtime.experiment_sandbox import (
                load_experiment_protocol_reference,
                load_model_invocation_set_reference,
                load_scan_scope_reference,
            )

            protocol = load_experiment_protocol_reference(
                kwargs["experiment_protocol_reference"],
                kwargs["authority_root"],
            )
            manifest = load_model_invocation_set_reference(
                kwargs["invocation_set_reference"],
                kwargs["authority_root"],
            )
            _scan_scope, scan_digest = load_scan_scope_reference(
                kwargs["scan_scope_reference"],
                kwargs["authority_root"],
            )
            evidence = {
                "evaluation_status": "passed",
                "evaluator_sha256": kwargs[
                    "evaluator_reference"
                ]["sha256"],
                "started_at": "2026-07-27T00:00:00Z",
                "finished_at": "2026-07-27T00:01:00Z",
                "taskpack_ids": sorted(
                    {
                        item["taskpack_id"]
                        for item in manifest["invocation_sets"]
                    }
                ),
                "experiment_protocol_reference_sha256": kwargs[
                    "experiment_protocol_reference"
                ]["sha256"],
                "scan_scope_sha256": scan_digest,
                "acceptance_command_sha256": (
                    canonical_json_sha256(
                        protocol["acceptance"]["command"]
                    )
                ),
                "acceptance_executable_sha256": "a" * 64,
                "provider_sandbox_reference_sha256": kwargs[
                    "provider_sandbox_reference"
                ]["sha256"],
                "failure_reason": None,
            }
            Path(kwargs["evidence_path"]).write_bytes(
                canonical_json_bytes(evidence) + b"\n"
            )
            return evidence

        def fake_seal(run_dir, bundle, **_kwargs):
            result_dir = Path(run_dir) / "results" / "terminal"
            digest = _publish_sealed_directory(
                result_dir,
                bundle,
                payload_name="result.json",
                digest_name="result.sha256",
                staging_prefix=".result-staging-",
            )
            return {
                "result_status": "sealed",
                "result_dir": str(result_dir),
                "bundle_sha256": digest,
                "bundle": bundle,
            }

        with patch(
            "agentteam_runtime.experiment_sandbox."
            "probe_gold_canary_denial",
            side_effect=_successful_namespace_probe,
        ), patch(
            "agentteam_runtime.experiment_modes."
            "run_trusted_argv_evaluator",
            side_effect=fake_evaluator,
        ), patch(
            "agentteam_runtime.experiment_modes."
            "seal_experiment_result_bundle",
            side_effect=fake_seal,
        ):
            yield

    def test_single_mode_receives_no_agentteam_task_context(self):
        class FakeGatedRunner:
            calls = []

            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                del lifecycle, timeout_seconds, environment
                type(self).calls.append(
                    {
                        "command": list(command),
                        "cwd": cwd,
                        "input_text": input_text,
                    }
                )
                self.command = list(command)

            def prepare(self):
                return ExecutionGroupIdentity.not_applicable()

            def permit_and_wait(self, **_kwargs):
                return ProviderExecution(
                    self.command,
                    0,
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 2,
                                "cached_input_tokens": 0,
                                "output_tokens": 1,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 3,
                            },
                        }
                    ),
                    "",
                )

            def abort_before_permit(self):
                return None

            def cleanup_after_terminal(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "single_codex")
            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.model_invocation."
                "SystemdGatedExecution",
                FakeGatedRunner,
            ):
                result = self._controller(fixture).execute(
                    SingleCodexModeAdapter()
                )

            self.assertEqual(result["provider_invocation_count"], 1)
            self.assertEqual(
                result["sealed_result"]["acceptance_status"],
                "passed",
            )
            self.assertIsNone(result["taskpack"])
            self.assertEqual(len(FakeGatedRunner.calls), 1)
            prompt = FakeGatedRunner.calls[0]["input_text"]
            self.assertNotIn("taskpack", prompt.lower())
            self.assertNotIn("repo map", prompt.lower())
            self.assertIn(
                str(fixture["snapshot"]),
                FakeGatedRunner.calls[0]["command"],
            )
            self.assertEqual(
                result["invocation_set_reference"]["schema_version"],
                "experiment_model_invocation_set_reference.v1",
            )

    def test_native_single_provider_uses_registered_live_contract(self):
        with self.assertRaises(TypeError):
            NativeSingleCodexProvider(
                codex_command=[
                    "codex",
                    "exec",
                    "--oss",
                    "--local-provider",
                    "ollama",
                ]
            )

        class FakeGatedRunner:
            command = None

            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                del lifecycle, cwd, input_text, timeout_seconds, environment
                type(self).command = list(command)

            def prepare(self):
                return ExecutionGroupIdentity.not_applicable()

            def permit_and_wait(self, **_kwargs):
                return ProviderExecution(
                    self.command,
                    0,
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 2,
                                "cached_input_tokens": 0,
                                "output_tokens": 1,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 3,
                            },
                        }
                    ),
                    "",
                )

            def abort_before_permit(self):
                return None

            def cleanup_after_terminal(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "single_codex")
            provider = NativeSingleCodexProvider()
            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.model_invocation."
                "SystemdGatedExecution",
                FakeGatedRunner,
            ):
                result = self._controller(fixture).execute(
                    SingleCodexModeAdapter(provider)
                )

            self.assertEqual(result["terminal_status"], "completed")
            self.assertIn(
                fixture["protocol"]["environment"]["model"],
                FakeGatedRunner.command,
            )
            self.assertIn(
                "model_reasoning_effort=high",
                FakeGatedRunner.command,
            )

    def test_single_mode_rejects_adapter_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "single_codex")

            class MisreportingAdapter:
                mode = "single_codex"

                def execute(self, _request):
                    raise AssertionError("substitute adapter must not run")

            with self.assertRaisesRegex(
                ExperimentModeError,
                "concrete protocol adapter",
            ):
                self._controller(fixture).execute(
                    MisreportingAdapter()
                )

    def test_controller_rejects_missing_sandbox_and_wrong_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "single_codex")
            with self.assertRaisesRegex(
                ExperimentModeError,
                "sandbox configuration",
            ):
                ExperimentModeController(
                    protocol=fixture["protocol"],
                    run_manifest=fixture["manifest"],
                    run_dir=fixture["run_dir"],
                    project_root=fixture["snapshot"],
                    authority_root=fixture["authority_root"],
                    controller_reference=fixture["controller"].reference,
                    sandbox_configuration=None,
                    common_finalizer=lambda **kwargs: kwargs,
                    runtime_release_identity=_release(),
                )
            with self.assertRaisesRegex(
                ExperimentModeError,
                "finalizer authority",
            ):
                ExperimentModeController(
                    protocol=fixture["protocol"],
                    run_manifest=fixture["manifest"],
                    run_dir=fixture["run_dir"],
                    project_root=fixture["snapshot"],
                    authority_root=fixture["authority_root"],
                    controller_reference=fixture["controller"].reference,
                    sandbox_configuration=fixture[
                        "sandbox_configuration"
                    ],
                    common_finalizer=lambda **kwargs: kwargs,
                    runtime_release_identity=_release(),
                )

            class WrongAdapter:
                mode = "agentteam_direct"

                def execute(self, _request):
                    raise AssertionError("must not execute")

            with self.assertRaisesRegex(
                ExperimentModeError,
                "adapter mode",
            ):
                self._controller(fixture).execute(WrongAdapter())

            first = self._controller(fixture)
            self.assertIsNotNone(first.sandbox_configuration_sha256)
            changed_configuration = copy.deepcopy(
                fixture["sandbox_configuration"]
            )
            changed_configuration["environment"]["DIFFERENT"] = "1"
            with self.assertRaisesRegex(
                ExperimentContractError,
                "already exists with different",
            ):
                ExperimentModeController(
                    protocol=fixture["protocol"],
                    run_manifest=fixture["manifest"],
                    run_dir=fixture["run_dir"],
                    project_root=fixture["snapshot"],
                    authority_root=fixture["authority_root"],
                    controller_reference=fixture["controller"].reference,
                    sandbox_configuration=changed_configuration,
                    common_finalizer=lambda **kwargs: kwargs,
                    runtime_release_identity=_release(),
                )

    def test_controller_rejects_different_protocol_budget_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "single_codex")
            changed = copy.deepcopy(fixture["protocol"])
            changed["budgets"]["max_total_tokens"] += 1
            wrong = create_experiment_controller(
                Path(tmp) / "wrong-protocol-controller",
                protocol_id=changed["experiment_id"],
                max_total_tokens=changed["budgets"][
                    "max_total_tokens"
                ],
                max_wall_time_seconds=changed["budgets"][
                    "max_wall_time_seconds"
                ],
                soft_warning_ratio=changed["budgets"][
                    "soft_warning_ratio"
                ],
                scored=changed["scored"],
                protocol_sha256=canonical_json_sha256(changed),
                operator_limits=changed["operator_limits"],
            )
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "differs from protocol",
            ):
                ExperimentModeController(
                    protocol=fixture["protocol"],
                    run_manifest=fixture["manifest"],
                    run_dir=fixture["run_dir"],
                    project_root=fixture["snapshot"],
                    authority_root=fixture["authority_root"],
                    controller_reference=wrong.reference,
                    sandbox_configuration=fixture[
                        "sandbox_configuration"
                    ],
                    common_finalizer=ExperimentCommonFinalizer(
                        evaluator_artifact=fixture["evaluator"],
                        runtime_release_identity=_release(),
                    ),
                    runtime_release_identity=_release(),
                )

    def test_execute_bound_rejects_unallocated_run_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            repository = _fixture_repository(root / "source")
            protocol = copy.deepcopy(_protocol())
            protocol["repository"] = repository["repository"]
            manifest = build_experiment_run_manifest(
                protocol,
                mode=protocol["mode_order"][0],
                repetition_index=0,
                stable_request_key="forged-bound-run",
            )
            run_dir = (
                root / "other-root" / "runs"
                / manifest["experiment_run_id"]
            )
            run_dir.mkdir(parents=True)
            protocol_path = root / "forged-protocol.json"
            protocol_path.write_bytes(
                canonical_json_bytes(protocol) + b"\n"
            )
            forged = {
                "run_dir": str(run_dir),
                "protocol_path": str(protocol_path),
                "run_manifest": manifest,
                "binding": {
                    "protocol_sha256": canonical_json_sha256(
                        protocol
                    ),
                    "runtime_release": _release(),
                },
            }
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            with self.assertRaises(ExperimentContractError):
                execute_bound_experiment_mode(
                    forged,
                    sandbox_configuration={},
                    adapter=AgentTeamDirectModeAdapter(root),
                    common_finalizer=ExperimentCommonFinalizer(
                        evaluator_artifact=evaluator,
                        runtime_release_identity=_release(),
                    ),
                )

    def test_execute_bound_accepts_allocation_authority(self):
        class FakeGatedRunner:
            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                del lifecycle, cwd, input_text, timeout_seconds, environment
                self.command = list(command)

            def prepare(self):
                return ExecutionGroupIdentity.not_applicable()

            def permit_and_wait(self, **_kwargs):
                return ProviderExecution(
                    self.command,
                    0,
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 2,
                                "cached_input_tokens": 0,
                                "output_tokens": 1,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 3,
                            },
                        }
                    ),
                    "",
                )

            def abort_before_permit(self):
                return None

            def cleanup_after_terminal(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            repository = _fixture_repository(root / "source")
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            protocol = copy.deepcopy(_protocol())
            protocol["repository"] = repository["repository"]
            protocol["seed"] = 0
            protocol["mode_order"] = [
                "single_codex",
                "agentteam_direct",
                "agentteam_full",
            ]
            protocol["evaluator"]["artifact_sha256"] = (
                hashlib.sha256(evaluator.read_bytes()).hexdigest()
            )
            allocation = allocate_experiment_run(
                root / "experiment",
                protocol,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="bound-production-path",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            credential = root / "credential.json"
            credential.write_text("{}\n", encoding="utf-8")
            canary = root / "gold-canary"
            canary.write_text("bound-canary\n", encoding="utf-8")
            sandbox_configuration = {
                "runtime_views": [
                    {"source": path, "target": path}
                    for path in ("/usr", "/lib", "/lib64", "/bin")
                    if Path(path).exists()
                ],
                "library_views": [],
                "credential_mounts": [
                    {
                        "source": str(credential),
                        "target": (
                            "/run/agentteam-credentials/provider.json"
                        ),
                    }
                ],
                "environment": {
                    "AGENTTEAM_CREDENTIAL_FILE": (
                        "/run/agentteam-credentials/provider.json"
                    )
                },
                "canary_path": str(canary),
            }
            with acquire_controller_lease(
                allocation["run_dir"],
                controller_id="competing-controller",
            ), self.assertRaises(ExperimentLeaseError):
                execute_bound_experiment_mode(
                    allocation,
                    sandbox_configuration=sandbox_configuration,
                    adapter=SingleCodexModeAdapter(),
                    common_finalizer=ExperimentCommonFinalizer(
                        evaluator_artifact=evaluator,
                        runtime_release_identity=_release(),
                    ),
                )
            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.model_invocation."
                "SystemdGatedExecution",
                FakeGatedRunner,
            ):
                result = execute_bound_experiment_mode(
                    allocation,
                    sandbox_configuration=sandbox_configuration,
                    adapter=SingleCodexModeAdapter(),
                    common_finalizer=ExperimentCommonFinalizer(
                        evaluator_artifact=evaluator,
                        runtime_release_identity=_release(),
                    ),
                )
            self.assertEqual(
                result["sealed_result"]["acceptance_status"],
                "passed",
            )

    def test_counterbalanced_mode_order_is_enforced_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "agentteam_direct")
            protocol = fixture["protocol"]
            root = Path(fixture["controller"].reference["controller_root"])
            first = build_experiment_run_manifest(
                protocol,
                mode=protocol["mode_order"][0],
                repetition_index=0,
                stable_request_key="order-first",
            )
            second = build_experiment_run_manifest(
                protocol,
                mode=protocol["mode_order"][1],
                repetition_index=0,
                stable_request_key="order-second",
            )
            _publish_mode_order_authority(root, protocol)
            with self.assertRaisesRegex(
                ExperimentModeError,
                "counterbalanced order",
            ):
                _begin_mode_execution(root, protocol, second)
            started = _begin_mode_execution(root, protocol, first)
            self.assertEqual(started["sequence_index"], 0)
            _complete_mode_execution(
                root,
                protocol,
                first,
                sealed_result={
                    "bundle_sha256": "d" * 64,
                    "terminal_status": "completed",
                    "acceptance_status": "passed",
                },
            )
            next_started = _begin_mode_execution(
                root,
                protocol,
                second,
            )
            self.assertEqual(next_started["sequence_index"], 1)
            changed = copy.deepcopy(protocol)
            changed["seed"] += 1
            with self.assertRaises(ExperimentContractError):
                _publish_mode_order_authority(root, changed)

    def test_direct_mode_verifies_digest_and_overrides_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "taskpack-repository").mkdir()
            taskpack_repository = _fixture_repository(
                root / "taskpack-repository"
            )
            draft = draft_taskpack_files(
                project_root=taskpack_repository["source"],
                goal="Update tracked.txt.",
                draft_root=root / "drafts",
                taskpack_id="direct-fixture",
                read_scope=["tracked.txt"],
                write_scope=["tracked.txt"],
                verification_command=["python3", "-m", "unittest"],
                role_routing=False,
            )
            frozen = freeze_taskpack(
                draft["taskpack_dir"],
                root / "frozen",
                expected_authoring_mode="direct_draft",
            )
            fixture = self._fixture(
                root / "mode",
                "agentteam_direct",
                direct_taskpack_sha256=frozen["manifest"][
                    "digest_sha256"
                ],
            )
            launches = []

            def launch(**kwargs):
                import inspect
                from agentteam_runtime.agentteam import (
                    _run_frozen_taskpack,
                )

                inspect.signature(_run_frozen_taskpack).bind_partial(
                    **kwargs
                )
                launches.append(kwargs)
                _complete_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="direct-worker",
                    taskpack_id="direct-fixture",
                )
                return {
                    "terminal_status": "completed",
                    "provider_invocation_count": 1,
                }

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=launch,
            ), patch(
                "agentteam_runtime.experiment_controller."
                "ExperimentController.operator_action_projection",
                return_value={
                    "operator_action_counts": {
                        "expected_operator_action": 1,
                        "corrective_intervention": 1,
                        "decision_escalation": 0,
                    },
                },
            ):
                result = self._controller(fixture).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )

            self.assertEqual(
                result["taskpack"]["digest_sha256"],
                fixture["protocol"]["direct_taskpack"]["sha256"],
            )
            self.assertEqual(
                load_experiment_result_bundle(fixture["run_dir"])[
                    "bundle"
                ]["operator_action_counts"],
                {
                    "expected_operator_action": 1,
                    "corrective_intervention": 1,
                    "decision_escalation": 0,
                },
            )
            self.assertEqual(
                launches[0]["trusted_project_root"],
                str(fixture["snapshot"]),
            )
            self.assertEqual(
                launches[0]["run_root"],
                str(fixture["run_dir"] / "agentteam-runtime"),
            )
            invalid_fixture = self._fixture(
                root / "invalid-candidate-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )
            invalid_candidate = (
                invalid_fixture["run_dir"] / "not-a-repository"
            )
            invalid_candidate.mkdir()

            def invalid_launch(**kwargs):
                _complete_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="invalid-candidate-worker",
                    taskpack_id="direct-fixture",
                )
                return {
                    "terminal_status": "completed",
                    "provider_invocation_count": 1,
                    "candidate_workspace": str(invalid_candidate),
                }

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=invalid_launch,
            ):
                invalid_result = self._controller(
                    invalid_fixture
                ).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            self.assertEqual(
                invalid_result["terminal_status"],
                "infrastructure_failed",
            )
            self.assertTrue(
                (
                    invalid_fixture["run_dir"]
                    / "results"
                    / "terminal"
                    / "result.json"
                ).is_file()
            )
            failed_terminals = list(
                (
                    Path(
                        invalid_fixture["controller"].reference[
                            "controller_root"
                        ]
                    )
                    / "mode-executions"
                ).glob("*.terminal.json")
            )
            self.assertEqual(len(failed_terminals), 1)
            self.assertEqual(
                json.loads(
                    failed_terminals[0].read_text(encoding="utf-8")
                )["outcome"],
                "sealed",
            )
            next_manifest = build_experiment_run_manifest(
                invalid_fixture["protocol"],
                mode=invalid_fixture["protocol"]["mode_order"][1],
                repetition_index=0,
                stable_request_key="after-failed-mode",
            )
            self.assertEqual(
                _begin_mode_execution(
                    invalid_fixture["controller"].reference[
                        "controller_root"
                    ],
                    invalid_fixture["protocol"],
                    next_manifest,
                )["sequence_index"],
                1,
            )
            retryable_fixture = self._fixture(
                root / "retryable-infrastructure-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )
            with patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=OSError("runtime unavailable"),
            ), self.assertRaises(OSError):
                self._controller(retryable_fixture).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            retryable_terminal = next(
                (
                    Path(
                        retryable_fixture["controller"].reference[
                            "controller_root"
                        ]
                    )
                    / "mode-executions"
                ).glob("*.terminal.json")
            )
            self.assertEqual(
                json.loads(
                    retryable_terminal.read_text(encoding="utf-8")
                )["outcome"],
                "retryable_infrastructure_failure",
            )
            retry = _begin_mode_execution(
                retryable_fixture["controller"].reference[
                    "controller_root"
                ],
                retryable_fixture["protocol"],
                retryable_fixture["manifest"],
            )
            self.assertEqual(retry["attempt_index"], 2)
            interrupted_fixture = self._fixture(
                root / "interrupted-candidate-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )
            interrupted_candidate, _branch = (
                create_independent_attempt_workspace(
                    interrupted_fixture["snapshot"],
                    interrupted_fixture["run_dir"]
                    / "candidate-workspaces",
                    "CANDIDATE-INTERRUPTED",
                    "WT-CANDIDATE-INTERRUPTED",
                )
            )
            (interrupted_candidate / "tracked.txt").write_text(
                "interrupted candidate change\n",
                encoding="utf-8",
            )

            def interrupted_launch(**kwargs):
                _complete_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="interrupted-worker",
                    taskpack_id="direct-fixture",
                )
                state_dir = (
                    Path(kwargs["run_root"])
                    / "direct-fixture"
                    / "state"
                )
                state_dir.mkdir(parents=True)
                (state_dir / "two_phase_scheduler_state.json").write_text(
                    json.dumps(
                        {
                            "integration_baseline": {
                                "integration_baseline_worktree_path": str(
                                    interrupted_candidate
                                ),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                raise OSError("runtime failed after candidate creation")

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=interrupted_launch,
            ):
                interrupted_result = self._controller(
                    interrupted_fixture
                ).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            interrupted_bundle = load_experiment_result_bundle(
                interrupted_fixture["run_dir"]
            )["bundle"]
            self.assertEqual(
                interrupted_result["candidate_workspace"],
                str(interrupted_candidate),
            )
            self.assertEqual(
                interrupted_result["taskpack"]["taskpack_id"],
                "direct-fixture",
            )
            self.assertIn(
                "tracked.txt",
                interrupted_bundle["changed_files"],
            )
            self.assertIn(
                "infrastructure_failure:OSError:"
                + hashlib.sha256(
                    b"runtime failed after candidate creation"
                ).hexdigest(),
                interrupted_bundle["regressions"],
            )
            orphan_fixture = self._fixture(
                root / "orphaned-provider-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )

            def orphan_launch(**kwargs):
                _start_orphaned_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="orphaned-worker",
                    taskpack_id="direct-fixture",
                )
                raise OSError("provider process died after durable start")

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=orphan_launch,
            ), patch(
                "agentteam_runtime.two_phase_scheduler."
                "_assess_persisted_execution_group",
                return_value={
                    "fence_status": "death_proven",
                    "proof": "deterministic_test_death_proof",
                },
            ):
                orphan_result = self._controller(
                    orphan_fixture
                ).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            self.assertEqual(
                orphan_result["terminal_status"],
                "infrastructure_failed",
            )
            orphan_terminal = next(
                (
                    orphan_fixture["authority_root"]
                    / "experiment_lifecycles"
                ).glob(
                    "*/model_invocations/*/terminal.json"
                )
            )
            self.assertEqual(
                json.loads(
                    orphan_terminal.read_text(encoding="utf-8")
                )["terminal_status"],
                "recovered_orphan",
            )
            self.assertFalse(
                orphan_fixture["controller"].snapshot()[
                    "budget_state"
                ]["usage_complete"]
            )
            preserved_fixture = self._fixture(
                root / "preserved-candidate-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )
            preserved_candidate, _branch = (
                create_independent_attempt_workspace(
                    preserved_fixture["snapshot"],
                    preserved_fixture["run_dir"]
                    / "candidate-workspaces",
                    "CANDIDATE-001",
                    "WT-CANDIDATE-001",
                )
            )
            (preserved_candidate / "tracked.txt").write_text(
                "candidate change\n",
                encoding="utf-8",
            )

            def preserved_launch(**kwargs):
                _complete_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="preserved-worker",
                    taskpack_id="direct-fixture",
                )
                return {
                    "terminal_status": "completed",
                    "provider_invocation_count": 1,
                    "candidate_workspace": str(preserved_candidate),
                }

            original_finalize = ExperimentCommonFinalizer.__call__
            finalizer_calls = {"count": 0}

            def flaky_finalize(finalizer, **kwargs):
                finalizer_calls["count"] += 1
                if finalizer_calls["count"] == 1:
                    raise OSError("first finalization failed")
                return original_finalize(finalizer, **kwargs)

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=preserved_launch,
            ), patch.object(
                ExperimentCommonFinalizer,
                "__call__",
                new=flaky_finalize,
            ):
                preserved_result = self._controller(
                    preserved_fixture
                ).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            preserved_bundle = load_experiment_result_bundle(
                preserved_fixture["run_dir"]
            )["bundle"]
            self.assertEqual(
                preserved_result["candidate_workspace"],
                str(preserved_candidate),
            )
            self.assertEqual(
                preserved_bundle["terminal_status"],
                "infrastructure_failed",
            )
            self.assertIn(
                "tracked.txt",
                preserved_bundle["changed_files"],
            )
            self.assertIn(
                "infrastructure_failure:OSError:"
                + hashlib.sha256(
                    b"first finalization failed"
                ).hexdigest(),
                preserved_bundle["regressions"],
            )
            (Path(frozen["frozen_taskpack_dir"]) / "README.md").write_text(
                "tampered\n",
                encoding="utf-8",
            )
            tampered_fixture = self._fixture(
                root / "tampered-mode",
                "agentteam_direct",
                direct_taskpack_sha256=fixture["protocol"][
                    "direct_taskpack"
                ]["sha256"],
            )
            with self.assertRaisesRegex(
                ExperimentModeError,
                "digest",
            ):
                self._controller(tampered_fixture).execute(
                    AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"],
                    )
                )
            execution_root = (
                Path(
                    tampered_fixture["controller"].reference[
                        "controller_root"
                    ]
                )
                / "mode-executions"
            )
            self.assertFalse(
                execution_root.exists()
                and any(execution_root.glob("*.started.json"))
            )

    def test_full_mode_authors_without_direct_taskpack_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp, "agentteam_full")
            calls = {"author": [], "freeze": [], "launch": []}
            authored_dir = Path(tmp) / "authored"
            authored_dir.mkdir()
            frozen_dir = Path(tmp) / "authored-frozen"
            frozen_dir.mkdir()

            def author(project_root, goal, draft_root, **kwargs):
                kwargs = {
                    **kwargs,
                    "project_root": project_root,
                    "goal": goal,
                    "draft_root": draft_root,
                }
                calls["author"].append(kwargs)
                self.assertEqual(
                    _registered_experiment_author_output(
                        Path(kwargs["project_root"]),
                        Path(kwargs["draft_root"]),
                        kwargs["author_invocation_context"],
                    ),
                    ".agentteam-author",
                )
                _complete_fake_experiment_invocation(
                    kwargs["author_invocation_context"],
                    kwargs["project_root"],
                    role="taskpack_author",
                )
                return {
                    "taskpack_dir": str(authored_dir),
                    "authoring_mode": "codex",
                }

            def freezer(taskpack_dir, frozen_root, **kwargs):
                kwargs = {
                    **kwargs,
                    "taskpack_dir": taskpack_dir,
                    "frozen_root": str(frozen_root),
                }
                calls["freeze"].append(kwargs)
                return {
                    "frozen_taskpack_dir": str(frozen_dir),
                    "manifest": {
                        "taskpack_id": "authored-fixture",
                        "digest_sha256": "6" * 64,
                    },
                }

            def launch(**kwargs):
                calls["launch"].append(kwargs)
                _complete_fake_runtime_invocation(
                    kwargs["experiment_runtime_context"],
                    kwargs["trusted_project_root"],
                    lifecycle_id="full-worker",
                    taskpack_id="authored-fixture",
                )
                return {
                    "terminal_status": "completed",
                    "provider_invocation_count": 2,
                }

            with self._mode_execution_boundary(), patch(
                "agentteam_runtime.taskpack_author."
                "draft_taskpack_from_goal",
                side_effect=author,
            ), patch(
                "agentteam_runtime.taskpack.freeze_taskpack",
                side_effect=freezer,
            ), patch(
                "agentteam_runtime.agentteam._run_frozen_taskpack",
                side_effect=launch,
            ):
                result = self._controller(fixture).execute(
                    AgentTeamFullModeAdapter()
                )

            self.assertEqual(result["taskpack"]["source"], "authored")
            self.assertEqual(
                calls["launch"][0]["run_root"],
                str(fixture["run_dir"] / "agentteam-runtime"),
            )
            self.assertNotEqual(
                calls["author"][0]["project_root"],
                str(fixture["snapshot"]),
            )
            self.assertEqual(
                _git(
                    calls["author"][0]["project_root"],
                    "rev-parse",
                    "HEAD",
                ).stdout.strip(),
                fixture["protocol"]["repository"]["commit"],
            )
            self.assertEqual(
                calls["author"][0]["author_runtime"],
                "codex",
            )
            self.assertEqual(
                calls["author"][0]["codex_model"],
                fixture["protocol"]["environment"]["model"],
            )
            serialized = json.dumps(calls["author"], sort_keys=True)
            self.assertNotIn(
                fixture["protocol"]["direct_taskpack"]["sha256"],
                serialized,
            )

    def test_independent_attempt_workspace_has_distinct_object_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            output = Path(tmp) / "run"
            workspace, _branch = create_independent_attempt_workspace(
                fixture["source"],
                output,
                "ATTEMPT-001",
                "WT-ATTEMPT-001",
            )

            source_common = Path(
                _git(
                    fixture["source"],
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ).stdout.strip()
            )
            workspace_common = Path(
                _git(
                    workspace,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ).stdout.strip()
            )
            self.assertNotEqual(
                source_common.resolve(),
                workspace_common.resolve(),
            )
            self.assertEqual(_git(workspace, "remote").stdout, "")
            self.assertFalse(
                (workspace_common / "objects" / "info" / "alternates").exists()
            )
            self.assertEqual(
                _git(workspace, "status", "--porcelain").stdout,
                "",
            )

    def test_registered_codex_command_binds_model_and_reasoning(self):
        policy = {
            "model": "codex-test-model",
            "reasoning_profile": "high",
        }
        _validate_registered_codex_command(
            [
                "codex",
                "exec",
                "-m",
                "codex-test-model",
                "-c",
                "model_reasoning_effort=high",
            ],
            policy,
        )
        with self.assertRaisesRegex(
            ModelInvocationIntegrityError,
            "model differs",
        ):
            _validate_registered_codex_command(
                [
                    "codex",
                    "exec",
                    "-m",
                    "wrong-model",
                    "-c",
                    "model_reasoning_effort=high",
                ],
                policy,
            )
        with self.assertRaisesRegex(
            ModelInvocationIntegrityError,
            "reasoning differs",
        ):
            _validate_registered_codex_command(
                [
                    "codex",
                    "exec",
                    "-m",
                    "codex-test-model",
                ],
                policy,
            )


class ExperimentContractSchemaTests(unittest.TestCase):
    def test_protocol_run_binding_and_state_schemas_are_executable(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="agentteam_direct",
            repetition_index=1,
            stable_request_key="stable-123",
        )

        self.assertIs(validate_experiment_protocol(protocol), protocol)
        self.assertIs(
            validate_experiment_run_manifest(manifest, protocol),
            manifest,
        )
        self.assertEqual(
            manifest["experiment_run_id"],
            derive_experiment_run_id(
                canonical_json_sha256(protocol),
                "agentteam_direct",
                1,
                "stable-123",
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            allocation = allocate_experiment_run(
                tmp,
                protocol,
                mode="agentteam_direct",
                repetition_index=1,
                stable_request_key="stable-123",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            self.assertEqual(
                validate_experiment_run_binding(allocation["binding"]),
                allocation["binding"],
            )
            self.assertEqual(
                validate_experiment_state(allocation["state"]),
                allocation["state"],
            )

    def test_protocol_requires_complete_three_mode_equal_input_contract(self):
        cases = {
            "missing-mode": lambda value: value["modes"].pop(),
            "duplicate-order": lambda value: value["mode_order"].__setitem__(
                0,
                value["mode_order"][1],
            ),
            "concurrent-provider-lanes": lambda value: value[
                "environment"
            ].__setitem__("max_inflight_model_invocations", 2),
            "missing-environment": lambda value: value["environment"].pop(
                "network_policy"
            ),
            "shell-acceptance": lambda value: value["acceptance"].__setitem__(
                "command",
                "python3 -m unittest",
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                protocol = _protocol()
                mutate(protocol)
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_protocol(protocol)

    def test_run_manifest_binds_mode_repetition_request_and_protocol(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="single_codex",
            repetition_index=0,
            stable_request_key="stable-001",
        )

        for field, value in {
            "protocol_sha256": "0" * 64,
            "mode": "agentteam_full",
            "repetition_index": 1,
            "stable_request_key": "stable-002",
        }.items():
            with self.subTest(field=field):
                drifted = dict(manifest)
                drifted[field] = value
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_run_manifest(drifted, protocol)

    def test_legacy_v1_manifest_remains_validation_only(self):
        legacy = {
            "schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
            "experiment_id": "legacy",
        }

        with self.assertRaisesRegex(ExperimentContractError, "validation-only"):
            ensure_executable_manifest(legacy)


class ExperimentAllocationTests(unittest.TestCase):
    def test_allocation_publishes_canonical_authority_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            root = Path(tmp)

            self.assertEqual(allocation["allocation_status"], "created")
            self.assertTrue(allocation["created"])
            self.assertEqual(allocation["provider_calls"], 0)
            self.assertEqual(allocation["target_mutations"], 0)
            protocol_path = Path(allocation["protocol_path"])
            manifest_path = Path(allocation["run_manifest_path"])
            binding_path = Path(allocation["run_dir"]) / "binding.json"
            state_path = Path(allocation["run_dir"]) / "state.json"
            request_path = Path(allocation["request_path"])
            for path in (
                protocol_path,
                manifest_path,
                binding_path,
                state_path,
                request_path,
            ):
                self.assertTrue(path.is_file(), path)

            self.assertEqual(
                protocol_path.read_bytes(),
                canonical_json_bytes(_protocol()) + b"\n",
            )
            self.assertEqual(
                json.loads(request_path.read_text(encoding="utf-8")),
                allocation["binding"],
            )
            self.assertFalse((Path(allocation["run_dir"]) / "repository").exists())
            self.assertEqual(allocation["state"]["status"], "prepared")
            self.assertEqual(
                allocation["binding"]["protocol_sha256"],
                canonical_json_sha256(_protocol()),
            )
            self.assertEqual(
                allocation["binding"]["run_manifest_sha256"],
                canonical_json_sha256(allocation["run_manifest"]),
            )
            self.assertEqual(
                sorted(path.name for path in (root / "protocols").iterdir()),
                [f"{allocation['protocol_sha256']}.json"],
            )

    def test_same_stable_request_returns_existing_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp)
            provider = Mock(name="provider")

            repeated = _allocate(tmp)
            if repeated["created"]:
                provider()

            self.assertEqual(repeated["allocation_status"], "existing")
            self.assertFalse(repeated["created"])
            self.assertEqual(repeated["experiment_run_id"], first["experiment_run_id"])
            self.assertEqual(repeated["binding"], first["binding"])
            self.assertEqual(repeated["provider_calls"], 0)
            provider.assert_not_called()
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 1)
            self.assertEqual(len(list((Path(tmp) / "requests").iterdir())), 1)

    def test_resume_reads_published_protocol_not_mutable_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "mutable-input.json"
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            allocation = allocate_experiment_run(
                root / "experiment-state",
                protocol_path,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="request-from-path",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            protocol_path.write_text('{"tampered":true}\n', encoding="utf-8")

            accepted = validate_resume_binding(
                allocation["run_dir"],
                runtime_release=_release(),
                repository=_protocol()["repository"],
            )

            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["protocol"], _protocol())

    def test_request_drift_fails_before_provider_or_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target-repository"
            target.mkdir()
            marker = target / "marker.txt"
            marker.write_text("unchanged\n", encoding="utf-8")
            _allocate(root)
            protocol_count = len(list((root / "protocols").iterdir()))
            provider = Mock(name="provider")
            drifted = _protocol()
            drifted["seed"] += 1

            with self.assertRaises(ExperimentContractError):
                result = allocate_experiment_run(
                    root,
                    drifted,
                    mode="single_codex",
                    repetition_index=0,
                    stable_request_key="request-001",
                    runtime_release=_release(),
                )
                if result["created"]:
                    provider()

            provider.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(len(list((root / "protocols").iterdir())), protocol_count)

    def test_distinct_request_keys_allocate_collision_safe_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp, "request-a")
            second = _allocate(tmp, "request-b")

            self.assertNotEqual(first["experiment_run_id"], second["experiment_run_id"])
            self.assertEqual(len(first["experiment_run_id"].split("-")[-1]), 64)
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 2)

    def test_tampered_published_protocol_fails_closed_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol_path = Path(allocation["protocol_path"])
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            provider = Mock(name="provider")

            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                result = _allocate(tmp)
                if result["created"]:
                    provider()

            provider.assert_not_called()


class ExperimentLeaseAndResumeTests(unittest.TestCase):
    def test_competing_controller_lease_fails_closed_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            first = acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-a",
                lease_id="lease-a",
                acquired_at="2026-07-27T00:00:01Z",
            )
            self.addCleanup(first.release)

            with self.assertRaises(ExperimentLeaseError):
                acquire_controller_lease(
                    allocation["run_dir"],
                    controller_id="controller-b",
                    lease_id="lease-b",
                )

            first.release()
            with acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-b",
                lease_id="lease-b",
            ) as second:
                self.assertTrue(second.held)
                self.assertEqual(second.record["controller_id"], "controller-b")
            self.assertFalse(second.held)

    def test_resume_accepts_exact_binding_and_rejects_all_identity_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol = _protocol()
            repository = protocol["repository"]
            release = _release()

            accepted = validate_resume_binding(
                allocation["run_dir"],
                protocol=protocol,
                run_manifest=allocation["run_manifest"],
                runtime_release=release,
                repository=repository,
                stable_request_key="request-001",
                experiment_run_id=allocation["experiment_run_id"],
                protocol_sha256=allocation["protocol_sha256"],
                run_manifest_sha256=allocation["run_manifest_sha256"],
            )
            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["provider_calls"], 0)

            drifted_protocol = copy.deepcopy(protocol)
            drifted_protocol["seed"] += 1
            drifted_manifest = build_experiment_run_manifest(
                protocol,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="another-request",
            )
            drifted_release = _release()
            drifted_release["release_id"] = "candidate-v2"
            drifted_repository = dict(repository)
            drifted_repository["source"] = "/srv/other/repository.git"
            drifted_object_format = {
                **repository,
                "commit": "8" * 64,
                "tree": "9" * 64,
                "git_object_format": "sha256",
            }
            cases = {
                "protocol": {"protocol": drifted_protocol},
                "run-manifest": {"run_manifest": drifted_manifest},
                "release": {"runtime_release": drifted_release},
                "repository": {"repository": drifted_repository},
                "object-format": {"repository": drifted_object_format},
                "request": {"stable_request_key": "another-request"},
            }
            for name, overrides in cases.items():
                with self.subTest(name=name):
                    arguments = {
                        "protocol": protocol,
                        "run_manifest": allocation["run_manifest"],
                        "runtime_release": release,
                        "repository": repository,
                        "stable_request_key": "request-001",
                    }
                    arguments.update(overrides)
                    provider = Mock(name=f"provider-{name}")
                    with self.assertRaises(ExperimentContractError):
                        result = validate_resume_binding(
                            allocation["run_dir"],
                            **arguments,
                        )
                        if result["resume_status"] == "accepted":
                            provider()
                    provider.assert_not_called()

    def test_resume_rejects_tampered_request_binding_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            request_path = Path(allocation["request_path"])
            request_path.write_text(
                json.dumps(allocation["binding"], indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                validate_resume_binding(allocation["run_dir"])

        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            state_path = Path(allocation["run_dir"]) / "state.json"
            state = dict(allocation["state"])
            state.update(
                {
                    "status": "budget_stopped",
                    "state_version": 2,
                    "updated_at": "2026-07-27T00:00:02Z",
                }
            )
            state_path.write_bytes(canonical_json_bytes(state) + b"\n")

            with self.assertRaisesRegex(ExperimentContractError, "cannot resume"):
                validate_resume_binding(allocation["run_dir"])


class ExperimentWorkspaceTests(unittest.TestCase):
    def test_allocates_independent_exact_commit_snapshot_and_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-fixture"
            run_dir.mkdir()

            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
                attested_at="2026-07-27T00:00:03Z",
            )

            snapshot = Path(allocation["snapshot_path"])
            attestation = load_clean_snapshot_attestation(
                allocation["attestation_path"]
            )
            self.assertEqual(
                validate_clean_snapshot_attestation(attestation),
                attestation,
            )
            self.assertEqual(
                _git(snapshot, "rev-parse", "HEAD").stdout.strip(),
                fixture["repository"]["commit"],
            )
            self.assertEqual(
                _git(snapshot, "rev-parse", "HEAD^{tree}").stdout.strip(),
                fixture["repository"]["tree"],
            )
            self.assertNotEqual(
                Path(attestation["snapshot_common_dir"]),
                Path(attestation["source_common_dir"]),
            )
            self.assertTrue(Path(attestation["snapshot_common_dir"]).is_dir())
            self.assertTrue(attestation["detached_head"])
            self.assertTrue(attestation["worktree_clean"])
            self.assertEqual(attestation["remotes"], [])
            self.assertEqual(attestation["alternates"], [])
            self.assertEqual(attestation["extra_refs"], [])
            self.assertFalse((snapshot / ".agentteam").exists())
            self.assertFalse((snapshot / "untracked.patch").exists())
            self.assertEqual(
                _git(snapshot, "symbolic-ref", "-q", "HEAD", check=False).returncode,
                1,
            )
            self.assertEqual(_git(snapshot, "remote").stdout, "")
            self.assertEqual(
                _git(
                    snapshot,
                    "for-each-ref",
                    "--format=%(refname)",
                ).stdout,
                "",
            )
            self.assertNotEqual(
                _git(
                    snapshot,
                    "cat-file",
                    "-e",
                    fixture["parent_commit"],
                    check=False,
                ).returncode,
                0,
            )

    def test_sha256_object_format_snapshot_preserves_exact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp, object_format="sha256")
            run_dir = Path(tmp) / "experiment-run-sha256"
            run_dir.mkdir()

            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )

            snapshot = Path(allocation["snapshot_path"])
            self.assertEqual(
                allocation["attestation"]["git_object_format"],
                "sha256",
            )
            self.assertEqual(
                len(allocation["attestation"]["head_commit"]),
                64,
            )
            self.assertEqual(
                _git(
                    snapshot,
                    "rev-parse",
                    "--show-object-format",
                ).stdout.strip(),
                "sha256",
            )
            self.assertNotEqual(
                _git(
                    snapshot,
                    "cat-file",
                    "-e",
                    fixture["parent_commit"],
                    check=False,
                ).returncode,
                0,
            )

    def test_exact_tree_and_bounded_inventory_fail_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            cases = {
                "tree-mismatch": {
                    "repository": {
                        **fixture["repository"],
                        "tree": "0" * 40,
                    },
                    "inventory_limit": 100,
                },
                "inventory-overflow": {
                    "repository": fixture["repository"],
                    "inventory_limit": 1,
                },
            }
            for name, case in cases.items():
                with self.subTest(name=name):
                    run_dir = Path(tmp) / f"experiment-run-{name}"
                    run_dir.mkdir()
                    with self.assertRaises(ExperimentWorkspaceError):
                        allocate_clean_snapshot(
                            run_dir,
                            case["repository"],
                            inventory_limit=case["inventory_limit"],
                        )
                    self.assertFalse((run_dir / "repository").exists())
                    self.assertFalse((run_dir / "clean-snapshot.json").exists())
                    self.assertEqual(
                        list(run_dir.glob(".repository-staging-*")),
                        [],
                    )

            preexisting_run = Path(tmp) / "experiment-run-preexisting"
            preexisting_snapshot = preexisting_run / "repository"
            preexisting_snapshot.mkdir(parents=True)
            marker = preexisting_snapshot / "operator-evidence.txt"
            marker.write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(ExperimentWorkspaceError, "already exists"):
                allocate_clean_snapshot(
                    preexisting_run,
                    fixture["repository"],
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_verification_denies_remote_alternate_and_extra_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-contamination"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            snapshot = Path(allocation["snapshot_path"])

            _git(snapshot, "remote", "add", "forbidden", fixture["repository"]["source"])
            with self.assertRaisesRegex(ExperimentWorkspaceError, "remote"):
                verify_clean_snapshot(snapshot, fixture["repository"])
            _git(snapshot, "remote", "remove", "forbidden")

            _git(
                snapshot,
                "update-ref",
                "refs/heads/forbidden",
                fixture["repository"]["commit"],
            )
            with self.assertRaisesRegex(ExperimentWorkspaceError, "extra Git refs"):
                verify_clean_snapshot(snapshot, fixture["repository"])
            _git(snapshot, "update-ref", "-d", "refs/heads/forbidden")

            common_dir = Path(
                _git(
                    snapshot,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ).stdout.strip()
            )
            alternates = common_dir / "objects" / "info" / "alternates"
            alternates.write_text(
                str(fixture["source"] / ".git" / "objects") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExperimentWorkspaceError, "alternates"):
                verify_clean_snapshot(snapshot, fixture["repository"])

    def test_symlink_escape_is_rejected_without_leaving_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp, escaping_symlink=True)
            run_dir = Path(tmp) / "experiment-run-symlink"
            run_dir.mkdir()

            with self.assertRaisesRegex(ExperimentWorkspaceError, "symlinks"):
                allocate_clean_snapshot(run_dir, fixture["repository"])

            self.assertFalse((run_dir / "repository").exists())
            self.assertFalse((run_dir / "clean-snapshot.json").exists())
            self.assertEqual(list(run_dir.glob(".repository-staging-*")), [])

    def test_cleanup_preserves_sealed_result_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-cleanup"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            result_dir = run_dir / "results" / "sealed-result"
            result_dir.mkdir(parents=True)
            result_path = result_dir / "result.json"
            result_path.write_text('{"status":"completed"}\n', encoding="utf-8")
            unsafe_result = (
                Path(allocation["snapshot_path"]) / "provider-result.json"
            )
            unsafe_result.write_text('{"unsafe":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentWorkspaceError,
                "outside the disposable snapshot",
            ):
                cleanup_clean_snapshot(
                    run_dir,
                    sealed_result_path=unsafe_result,
                )
            self.assertTrue(Path(allocation["snapshot_path"]).is_dir())
            unsafe_result.unlink()

            cleanup = cleanup_clean_snapshot(
                run_dir,
                sealed_result_path=result_dir,
            )

            self.assertEqual(cleanup["cleanup_status"], "removed")
            self.assertTrue(cleanup["result_preserved"])
            self.assertFalse(Path(allocation["snapshot_path"]).exists())
            self.assertEqual(
                result_path.read_text(encoding="utf-8"),
                '{"status":"completed"}\n',
            )
            self.assertEqual(
                cleanup["sealed_result_sha256"],
                cleanup["sealed_result_sha256_after_cleanup"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-cleanup-failure"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            result_path = run_dir / "sealed-result.json"
            result_path.write_text('{"status":"failed"}\n', encoding="utf-8")

            with patch(
                "agentteam_runtime.experiment_workspace.shutil.rmtree",
                side_effect=OSError("simulated cleanup failure"),
            ):
                cleanup = cleanup_clean_snapshot(
                    run_dir,
                    sealed_result_path=result_path,
                )

            self.assertEqual(cleanup["cleanup_status"], "failed")
            self.assertIn("simulated cleanup failure", cleanup["error"])
            self.assertTrue(Path(allocation["snapshot_path"]).is_dir())
            self.assertEqual(
                result_path.read_text(encoding="utf-8"),
                '{"status":"failed"}\n',
            )


class ExperimentSandboxTests(unittest.TestCase):
    def test_evaluator_execution_uses_digest_bound_memory_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            evaluator = Path(tmp) / "evaluator.py"
            original = b"#!/usr/bin/python3\nraise SystemExit(0)\n"
            evaluator.write_bytes(original)
            evaluator.chmod(0o700)
            digest = hashlib.sha256(original).hexdigest()

            content = _read_digest_bound_evaluator(
                evaluator,
                digest,
            )
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(91)\n",
                encoding="utf-8",
            )
            self.assertEqual(content, original)

    def test_candidate_repository_must_descend_from_certified_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            repository = fixture["repository"]
            baseline = fixture["repository_identity"]
            state = _candidate_repository_state(repository, baseline)
            self.assertEqual(state["baseline_commit"], baseline["commit"])
            fsmonitor_marker = Path(tmp) / "fsmonitor-executed"
            fsmonitor = Path(tmp) / "fsmonitor.sh"
            fsmonitor.write_text(
                "#!/bin/sh\n"
                f"touch {str(fsmonitor_marker)!r}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fsmonitor.chmod(0o700)
            _git(repository, "config", "core.fsmonitor", str(fsmonitor))
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "Git config contains undeclared behavior",
            ):
                _candidate_repository_state(repository, baseline)
            self.assertFalse(fsmonitor_marker.exists())
            _git(repository, "config", "--unset", "core.fsmonitor")
            tracked = repository / "tracked.txt"
            tracked.write_text("first dirty value\n", encoding="utf-8")
            first_dirty = _candidate_repository_state(repository, baseline)
            tracked.write_text("second dirty value\n", encoding="utf-8")
            second_dirty = _candidate_repository_state(repository, baseline)
            self.assertEqual(
                first_dirty["tracked_status_sha256"],
                second_dirty["tracked_status_sha256"],
            )
            self.assertNotEqual(
                first_dirty["working_tree_sha256"],
                second_dirty["working_tree_sha256"],
            )
            (repository / "untracked.txt").write_text(
                "untracked\n",
                encoding="utf-8",
            )
            with_untracked = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                second_dirty["working_tree_sha256"],
                with_untracked["working_tree_sha256"],
            )
            (repository / "untracked.txt").chmod(0o755)
            executable_untracked = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_untracked["working_tree_sha256"],
                executable_untracked["working_tree_sha256"],
            )
            (repository / ".git" / "info" / "exclude").write_text(
                "ignored.txt\n",
                encoding="utf-8",
            )
            (repository / "ignored.txt").write_text(
                "ignored content\n",
                encoding="utf-8",
            )
            with_ignored = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                executable_untracked["working_tree_sha256"],
                with_ignored["working_tree_sha256"],
            )
            empty_directory = repository / "empty-directory"
            empty_directory.mkdir()
            with_empty_directory = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["working_tree_sha256"],
                with_empty_directory["working_tree_sha256"],
            )
            empty_directory.rmdir()
            git_config = repository / ".git" / "config"
            original_git_config = git_config.read_bytes()
            _git(repository, "config", "user.name", "Changed Safe Name")
            with_git_config = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["git_control_sha256"],
                with_git_config["git_control_sha256"],
            )
            git_config.write_bytes(original_git_config)
            hook = repository / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o700)
            with_git_hook = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["git_control_sha256"],
                with_git_hook["git_control_sha256"],
            )
            hook.unlink()
            _git(repository, "reset", "--quiet", "--hard", baseline["commit"])
            (repository / "untracked.txt").unlink()
            (repository / "ignored.txt").unlink()

            _git(repository, "checkout", "--quiet", "--orphan", "unrelated")
            _git(repository, "rm", "--quiet", "-rf", ".")
            (repository / "unrelated.txt").write_text(
                "unrelated\n",
                encoding="utf-8",
            )
            _git(repository, "add", "unrelated.txt")
            _git(repository, "commit", "--quiet", "-m", "unrelated")
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "does not descend",
            ):
                _candidate_repository_state(repository, baseline)

    def test_candidate_repository_requires_standalone_git_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            linked = root / "linked-worktree"
            _git(
                fixture["repository"],
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(linked),
                fixture["repository_identity"]["commit"],
            )
            self.assertTrue((linked / ".git").is_file())
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "candidate Git control directory must be a directory",
            ):
                _candidate_repository_state(
                    linked,
                    fixture["repository_identity"],
                )

    def test_provider_descriptor_rejects_non_system_bubblewrap_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            fake_bwrap = Path(tmp) / "bwrap"
            shutil.copy2("/usr/bin/bwrap", fake_bwrap)
            fake_bwrap.chmod(0o700)
            forged = copy.deepcopy(fixture["descriptor"])
            forged["bwrap_path"] = str(fake_bwrap)
            forged["bwrap_sha256"] = hashlib.sha256(
                fake_bwrap.read_bytes()
            ).hexdigest()
            forged["policy_sha256"] = _sandbox_policy_sha256(forged)

            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "identity is unavailable or changed",
            ):
                validate_provider_sandbox_descriptor(forged)

    def test_provider_namespace_has_only_declared_views_and_bounded_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            descriptor = fixture["descriptor"]

            serialized = json.dumps(descriptor, sort_keys=True)
            self.assertNotIn(str(fixture["canary"]), serialized)
            self.assertNotIn(
                fixture["canary"].read_text(encoding="utf-8"),
                serialized,
            )
            self.assertEqual(len(descriptor["credential_views"]), 1)
            credential = descriptor["credential_views"][0]
            self.assertFalse(credential["writable"])
            self.assertEqual(credential["file_count"], 1)
            self.assertLessEqual(credential["total_bytes"], 1024 * 1024)

            prepared = prepare_provider_launch(
                descriptor,
                [str(Path(sys.executable).resolve()), "-c", "print('provider')"],
                cwd=fixture["repository"],
            )
            command = list(prepared.command)
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("--bind", command)
            self.assertIn("--ro-bind", command)
            self.assertEqual(prepared.cwd, "/")
            self.assertEqual(
                prepared.environment["AGENTTEAM_CREDENTIAL_FILE"],
                "/run/agentteam-credentials/provider.json",
            )
            self.assertNotIn(str(fixture["canary"]), " ".join(command))

            fake_python = Path(tmp) / "python3"
            fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_python.chmod(0o700)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "targets must not overlap",
            ):
                build_provider_sandbox_descriptor(
                    fixture["repository"],
                    runtime_views=[
                        {"source": "/usr", "target": "/usr"},
                        {
                            "source": str(fake_python),
                            "target": "/usr/bin/python3",
                        },
                    ],
                    bwrap_path="/usr/bin/bwrap",
                    forbidden_paths=[fixture["canary"]],
                )
            shadowing = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": "/usr", "target": "/runtime-usr"},
                    {
                        "source": str(fake_python),
                        "target": "/usr/bin/python3",
                    },
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "executable is not approved",
            ):
                _approved_acceptance_executable(
                    "/usr/bin/python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=shadowing,
                )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "must be an absolute path",
            ):
                _approved_acceptance_executable(
                    "python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=fixture["descriptor"],
                )
            symlink_runtime = Path(tmp) / "symlink-runtime"
            (symlink_runtime / "bin").mkdir(parents=True)
            os.symlink(
                "/runtime-usr/bin/python3.12",
                symlink_runtime / "bin" / "python3",
            )
            symlink_shadowing = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": "/usr", "target": "/runtime-usr"},
                    {"source": str(symlink_runtime), "target": "/usr"},
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "contains a symlink",
            ):
                _approved_acceptance_executable(
                    "/usr/bin/python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=symlink_shadowing,
                )
            drift_source = Path(tmp) / "runtime-drift"
            drift_source.mkdir()
            drift_descriptor = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": str(drift_source), "target": "/runtime-drift"},
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            original_source = Path(tmp) / "runtime-drift-original"
            drift_source.rename(original_source)
            os.symlink(str(Path(tmp)), drift_source)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "canonical non-symlink path",
            ):
                validate_provider_sandbox_descriptor(
                    drift_descriptor,
                    require_namespace_evidence=False,
                )
            mutable_runtime = Path(tmp) / "mutable-runtime"
            mutable_runtime.mkdir()
            mutable_tool = mutable_runtime / "tool"
            mutable_tool.write_text("first\n", encoding="utf-8")
            mutable_descriptor = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {
                        "source": str(mutable_runtime),
                        "target": "/mutable-runtime",
                    },
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            mutable_tool.write_text("second\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "identity is unavailable or changed",
            ):
                validate_provider_sandbox_descriptor(
                    mutable_descriptor,
                    require_namespace_evidence=False,
                )

    def test_evaluator_mount_or_inconclusive_probe_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "evaluator-only path overlaps",
            ):
                build_provider_sandbox_descriptor(
                    fixture["repository"],
                    runtime_views=[fixture["canary"].parent],
                    bwrap_path=fixture["descriptor"]["bwrap_path"],
                    forbidden_paths=[fixture["canary"]],
                )

            completed = subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    '{"content_readable":false,"path_visible":false}'
                ),
                stderr="",
            )
            probe = probe_gold_canary_denial(
                fixture["uncertified_descriptor"],
                fixture["canary"],
                runner=Mock(return_value=completed),
                probe_python=str(Path(sys.executable).resolve()),
            )
            self.assertEqual(probe["denial_status"], "denied")
            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "inconclusive",
            ):
                probe_gold_canary_denial(
                    fixture["uncertified_descriptor"],
                    fixture["canary"],
                    runner=Mock(
                        return_value=subprocess.CompletedProcess(
                            [],
                            0,
                            stdout="not-json",
                            stderr="",
                        )
                    ),
                    probe_python=str(Path(sys.executable).resolve()),
                )

    def test_real_bwrap_canary_denial_when_runner_supports_namespaces(self):
        bwrap = shutil.which("bwrap")
        if not bwrap or not sys.platform.startswith("linux"):
            self.skipTest("real bubblewrap is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            canary = root / "evaluator-only" / "gold-canary"
            canary.parent.mkdir()
            canary.write_text("real-bwrap-canary", encoding="utf-8")
            views = [
                {"source": path, "target": path}
                for path in ("/usr", "/lib", "/lib64", "/bin")
                if Path(path).exists()
            ]
            descriptor = build_provider_sandbox_descriptor(
                repository,
                runtime_views=views,
                bwrap_path=bwrap,
                forbidden_paths=[canary],
            )
            try:
                evidence = probe_gold_canary_denial(
                    descriptor,
                    canary,
                    probe_python="/usr/bin/python3",
                )
            except ExperimentSandboxUnavailable:
                if os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") == "1":
                    raise
                self.skipTest("runner disallows unprivileged bubblewrap")
            self.assertEqual(evidence["denial_status"], "denied")
            self.assertFalse(evidence["path_visible"])
            self.assertFalse(evidence["content_readable"])

    def test_real_systemd_evaluator_contains_detached_child_when_required(self):
        if os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1":
            self.skipTest("real systemd evaluator probe is opt-in")
        if not shutil.which("systemd-run") or not shutil.which("systemctl"):
            self.fail("systemd evaluator probe was required but is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_environment = {
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "/tmp",
            }
            script = (
                "import json,os,pathlib,subprocess\n"
                "child=subprocess.Popen(['/bin/sleep','60'],"
                "start_new_session=True)\n"
                "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                "pathlib.Path('environment.json').write_text("
                "json.dumps(dict(os.environ),sort_keys=True))\n"
            )
            execution = _run_bounded_argv(
                [str(Path(sys.executable).resolve()), "-c", script],
                cwd=root,
                environment=expected_environment,
                timeout_seconds=10,
                max_output_bytes=4096,
                cpu_limit=1,
                memory_limit_bytes=128 * 1024 * 1024,
            )
            child_pid = int((root / "child.pid").read_text(encoding="utf-8"))
            time.sleep(0.1)
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_alive = False
            else:
                child_alive = True
                os.kill(child_pid, 9)
            self.assertFalse(child_alive)
            self.assertEqual(
                json.loads(
                    (root / "environment.json").read_text(encoding="utf-8")
                ),
                expected_environment,
            )
            self.assertEqual(
                execution["execution_boundary"],
                "systemd_user_transient_service",
            )
            self.assertTrue(execution["systemd_unit"].endswith(".service"))

    def test_real_systemd_bwrap_contains_detached_candidate_when_required(self):
        if (
            os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1"
            or os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") != "1"
        ):
            self.skipTest("real systemd plus bwrap probe is opt-in")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            evaluator = root / "contract-evaluator.py"
            evaluator_content = (
                b"#!/usr/bin/python3\n"
                b"import sys\n"
                b"raise SystemExit(0 if len(sys.argv) >= 3 "
                b"and sys.argv[1] == '--' else 64)\n"
            )
            evaluator.write_bytes(evaluator_content)
            evaluator.chmod(0o700)
            child_started_path = fixture["repository"] / "child-started"
            child_survived_path = fixture["repository"] / "child-survived"
            git_writable_path = fixture["repository"] / "git-writable"
            git_readonly_path = fixture["repository"] / "git-readonly"
            git_probe_path = fixture["repository"] / ".git" / "write-probe"
            child_script = (
                "import pathlib,time;"
                f"pathlib.Path({str(child_started_path)!r}).write_text('1');"
                "time.sleep(4);"
                f"pathlib.Path({str(child_survived_path)!r}).write_text('1')"
            )
            acceptance = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "import pathlib,subprocess,time;"
                    "\ntry:\n"
                    f" pathlib.Path({str(git_probe_path)!r}).write_text('1')\n"
                    "except OSError:\n"
                    f" pathlib.Path({str(git_readonly_path)!r}).write_text('1')\n"
                    "else:\n"
                    f" pathlib.Path({str(git_writable_path)!r}).write_text('1')\n"
                    "child=subprocess.Popen("
                    f"[{str(Path(sys.executable).resolve())!r},"
                    f"'-c',{child_script!r}],"
                    "start_new_session=True);"
                    f"pathlib.Path({str(child_started_path)!r})."
                    "write_text(str(child.pid));"
                    "time.sleep(60)"
                ),
            ]
            prepared = prepare_candidate_evaluation_launch(
                fixture["descriptor"],
                evaluator,
                hashlib.sha256(evaluator_content).hexdigest(),
                acceptance,
                cwd=fixture["repository"],
            )
            try:
                execution = _run_bounded_argv(
                    list(prepared.command),
                    cwd=prepared.cwd,
                    environment=prepared.environment,
                    timeout_seconds=2,
                    max_output_bytes=4096,
                    cpu_limit=1,
                    memory_limit_bytes=128 * 1024 * 1024,
                    input_bytes=evaluator_content,
                )
            except ExperimentSandboxUnavailable:
                self.fail("required real systemd plus bwrap probe is unavailable")
            self.assertTrue(execution["timed_out"], execution)
            self.assertTrue(child_started_path.is_file())
            self.assertTrue(git_readonly_path.is_file())
            self.assertFalse(git_writable_path.exists())
            time.sleep(2.5)
            self.assertFalse(child_survived_path.exists())

    def test_real_bwrap_main_exit_does_not_leave_child_when_required(self):
        if (
            os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1"
            or os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") != "1"
        ):
            self.skipTest("real systemd plus bwrap probe is opt-in")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            evaluator = root / "contract-evaluator.py"
            evaluator_content = b"#!/usr/bin/python3\nraise SystemExit(0)\n"
            evaluator.write_bytes(evaluator_content)
            evaluator.chmod(0o700)
            survived_path = fixture["repository"] / "detached-survived"
            child_script = (
                "import pathlib,time;"
                "time.sleep(2);"
                f"pathlib.Path({str(survived_path)!r}).write_text('1')"
            )
            acceptance = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "import subprocess;"
                    "subprocess.Popen("
                    f"[{str(Path(sys.executable).resolve())!r},"
                    f"'-c',{child_script!r}],"
                    "start_new_session=True)"
                ),
            ]
            prepared = prepare_candidate_evaluation_launch(
                fixture["descriptor"],
                evaluator,
                hashlib.sha256(evaluator_content).hexdigest(),
                acceptance,
                cwd=fixture["repository"],
            )
            execution = _run_bounded_argv(
                list(prepared.command),
                cwd=prepared.cwd,
                environment=prepared.environment,
                timeout_seconds=5,
                max_output_bytes=4096,
                cpu_limit=1,
                memory_limit_bytes=128 * 1024 * 1024,
                input_bytes=evaluator_content,
            )
            self.assertFalse(execution["timed_out"], execution)
            self.assertEqual(execution["returncode"], 0, execution)
            time.sleep(2.5)
            self.assertFalse(survived_path.exists())

    def test_provider_environment_rejects_canary_content_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            canary = root / "evaluator-only" / "gold-canary"
            canary.parent.mkdir()
            canary.write_text("provider-must-not-see-this", encoding="utf-8")
            digest = hashlib.sha256(canary.read_bytes()).hexdigest()
            bwrap = Path("/usr/bin/bwrap")
            for leaked_value in (canary.read_text(encoding="utf-8"), digest):
                with self.subTest(leaked_value=leaked_value):
                    with self.assertRaisesRegex(
                        ExperimentSandboxError,
                        "evaluator-only material",
                    ):
                        build_provider_sandbox_descriptor(
                            repository,
                            runtime_views=[Path(sys.executable).resolve()],
                            environment={"LEAKED_GOLD": leaked_value},
                            bwrap_path=bwrap,
                            forbidden_paths=[canary],
                        )

    def test_common_model_invocation_policy_wraps_supported_and_fake_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            captures = []

            class FakeGatedRunner:
                def __init__(
                    self,
                    lifecycle,
                    command,
                    *,
                    cwd,
                    input_text,
                    timeout_seconds,
                    environment,
                ):
                    captures.append(
                        {
                            "command": command,
                            "cwd": cwd,
                            "environment": environment,
                        }
                    )

                def prepare(self):
                    return ExecutionGroupIdentity.not_applicable()

                def permit_and_wait(self, **_kwargs):
                    return ProviderExecution([], 0, "", "")

                def abort_before_permit(self):
                    return None

                def cleanup_after_terminal(self):
                    return None

            supported_root = Path(tmp) / "supported-authority"
            supported_root.mkdir()
            supported_lifecycle_root = experiment_lifecycle_authority_root(
                supported_root,
                "supported",
            )
            supported_reference = _publish_test_sandbox_reference(
                supported_root,
                fixture,
            )
            supported_context = _model_context(
                supported=True,
                sandbox_reference=supported_reference,
            )
            supported_context["experiment_authority_root"] = str(
                supported_root
            )
            supported = ModelInvocationCall(
                supported_lifecycle_root,
                supported_context,
                supported=True,
                systemd_runner_factory=FakeGatedRunner,
            )
            supported.execute(
                [str(Path(sys.executable).resolve()), "-c", "print('supported')"],
                cwd=fixture["repository"],
                input_text="prompt",
                timeout_seconds=10,
            )
            self.assertTrue(supported.lifecycle.started_path.is_file())
            start_record = json.loads(
                supported.lifecycle.started_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                start_record["experiment_sandbox_policy_sha256"],
                fixture["descriptor"]["policy_sha256"],
            )
            self.assertEqual(
                start_record["experiment_sandbox_reference_sha256"],
                supported_reference["sha256"],
            )
            self.assertEqual(captures[0]["cwd"], "/")
            self.assertEqual(
                captures[0]["command"][0],
                fixture["descriptor"]["bwrap_path"],
            )
            self.assertEqual(
                captures[0]["environment"],
                fixture["descriptor"]["environment"],
            )

            fake_root = Path(tmp) / "fake-authority"
            fake_root.mkdir()
            fake_lifecycle_root = experiment_lifecycle_authority_root(
                fake_root,
                "fake",
            )
            fake_reference = _publish_test_sandbox_reference(
                fake_root,
                fixture,
            )
            fake_context = _model_context(
                supported=False,
                sandbox_reference=fake_reference,
            )
            fake_context["experiment_authority_root"] = str(fake_root)
            fake = ModelInvocationCall(
                fake_lifecycle_root,
                fake_context,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation._run_bounded_process",
                return_value=ProviderExecution([], 0, "", ""),
            ) as bounded:
                fake.execute(
                    [str(Path(sys.executable).resolve()), "-c", "print('fake')"],
                    cwd=fixture["repository"],
                    input_text="prompt",
                    timeout_seconds=10,
                )
            self.assertEqual(
                bounded.call_args.args[0][0],
                fixture["descriptor"]["bwrap_path"],
            )
            self.assertEqual(
                bounded.call_args.kwargs["environment"],
                fixture["descriptor"]["environment"],
            )

    def test_mode_authority_requires_registered_launch_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            authority_root = root / "mode-authority"
            protocol = _sandbox_protocol(fixture)
            manifest = build_experiment_run_manifest(
                protocol,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="registered-launch",
            )
            controller = create_experiment_controller(
                authority_root,
                protocol_id=protocol["experiment_id"],
                max_total_tokens=protocol["budgets"][
                    "max_total_tokens"
                ],
                max_wall_time_seconds=protocol["budgets"][
                    "max_wall_time_seconds"
                ],
                soft_warning_ratio=protocol["budgets"][
                    "soft_warning_ratio"
                ],
                scored=protocol["scored"],
                protocol_sha256=canonical_json_sha256(protocol),
                operator_limits=protocol["operator_limits"],
            )
            model_policy = {
                "backend": "codex",
                "codex_cli_version": protocol["environment"][
                    "codex_cli_version"
                ],
                "model": protocol["environment"]["model"],
                "reasoning_profile": protocol["environment"][
                    "reasoning_profile"
                ],
                "service_configuration_sha256": protocol[
                    "environment"
                ]["service_configuration_sha256"],
                "sandbox_policy": protocol["environment"][
                    "sandbox_policy"
                ],
                "permission_policy": protocol["environment"][
                    "permission_policy"
                ],
                "network_policy": protocol["environment"][
                    "network_policy"
                ],
                "tool_allowlist": protocol["environment"][
                    "tool_allowlist"
                ],
                "max_inflight_model_invocations": 1,
            }
            publish_experiment_mode_authority(
                authority_root,
                experiment_run_id=manifest["experiment_run_id"],
                protocol_sha256=manifest["protocol_sha256"],
                run_manifest_sha256=canonical_json_sha256(manifest),
                mode=manifest["mode"],
                model_policy=model_policy,
                controller_reference=controller.reference,
                sandbox_configuration_sha256="8" * 64,
            )
            lifecycle_root = experiment_lifecycle_authority_root(
                authority_root,
                "single-call",
            )
            context = _model_context(
                supported=False,
                sandbox_reference=None,
            )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "registration is unavailable",
            ):
                ModelInvocationCall(
                    lifecycle_root,
                    context,
                    supported=False,
                )

            sandbox_reference = _publish_test_sandbox_reference(
                authority_root,
                fixture,
            )
            publish_experiment_launch_registration(
                authority_root,
                lifecycle_root,
                experiment_run_id=manifest["experiment_run_id"],
                protocol_sha256=manifest["protocol_sha256"],
                run_manifest_sha256=canonical_json_sha256(manifest),
                mode="single_codex",
                usage_stage="single_codex",
                taskpack_id="SINGLE-CODEX-NONE",
                workspace_root=fixture["repository"],
                sandbox_reference=sandbox_reference,
                controller_reference=controller.reference,
                model_policy=model_policy,
            )
            context.update(
                {
                    "run_id": manifest["experiment_run_id"],
                    "taskpack_id": "SINGLE-CODEX-NONE",
                    "usage_stage": "single_codex",
                    "model": model_policy["model"],
                    "reasoning_profile": model_policy[
                        "reasoning_profile"
                    ],
                }
            )
            context.pop("experiment_sandbox_reference", None)
            invocation = ModelInvocationCall(
                lifecycle_root,
                context,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation._run_bounded_process",
                return_value=ProviderExecution([], 0, "", ""),
            ):
                execution = invocation.execute(
                    [str(Path(sys.executable).resolve()), "-c", "pass"],
                    cwd=fixture["repository"],
                    input_text="prompt",
                    timeout_seconds=10,
                )
            invocation.finalize("completed", execution)
            started = json.loads(
                invocation.lifecycle.started_path.read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                started["experiment_run_id"],
                manifest["experiment_run_id"],
            )
            self.assertEqual(started["experiment_mode"], "single_codex")
            self.assertEqual(
                started["reasoning_profile"],
                model_policy["reasoning_profile"],
            )
            invocation_reference = (
                publish_registered_model_invocation_set_reference(
                    authority_root,
                    manifest["experiment_run_id"],
                )
            )
            self.assertTrue(
                Path(invocation_reference["path"]).is_file()
            )
            experiment_lifecycle_authority_root(
                authority_root,
                "unconsumed-call",
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "launch registration",
            ):
                publish_registered_model_invocation_set_reference(
                    authority_root,
                    manifest["experiment_run_id"],
                    reference_id="model-invocation-set-with-extra",
                )

            wrong = dict(context)
            wrong["model"] = "another-model"
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "caller context differs",
            ):
                ModelInvocationCall(
                    lifecycle_root,
                    wrong,
                    supported=False,
                )

    def test_sandbox_publication_requires_fresh_probe_and_valid_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            tampered_environment = copy.deepcopy(fixture["descriptor"])
            tampered_environment["environment"]["UNDECLARED_SECRET"] = "unsafe"
            authority = Path(tmp) / "authority-valid"
            authority.mkdir()
            reference = _publish_test_sandbox_reference(authority, fixture)
            self.assertTrue(Path(reference["path"]).is_file())

            invalid_authority = Path(tmp) / "authority-invalid"
            invalid_authority.mkdir()
            with self.assertRaises(ExperimentSandboxError):
                publish_provider_sandbox_reference(
                    invalid_authority,
                    tampered_environment,
                    fixture["canary"],
                )

    def test_required_experiment_sandbox_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = _model_context(
                supported=False,
                sandbox_reference=None,
            )
            context["experiment_sandbox_required"] = True
            invocation = ModelInvocationCall(
                Path(tmp) / "authority",
                context,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ModelInvocationIntegrityError,
                    "sandbox is required",
                ):
                    invocation.execute(
                        [str(Path(sys.executable).resolve()), "-c", "print('unsafe')"],
                        cwd=tmp,
                        input_text="prompt",
                        timeout_seconds=10,
                    )
            popen.assert_not_called()

    def test_sandbox_authority_must_be_explicit_and_provider_invisible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            authority = root / "authority"
            authority.mkdir()
            reference = _publish_test_sandbox_reference(
                authority,
                fixture,
            )
            missing_authority = _model_context(
                supported=False,
                sandbox_reference=reference,
            )
            invocation = ModelInvocationCall(
                authority / "missing-authority-output",
                missing_authority,
                supported=False,
            )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "authority root is required",
            ):
                invocation.execute(
                    [str(Path(sys.executable).resolve()), "-c", "pass"],
                    cwd=fixture["repository"],
                    input_text="",
                    timeout_seconds=10,
                )

            visible_authority = fixture["repository"] / "visible-authority"
            visible_authority.mkdir()
            visible_lifecycle_root = experiment_lifecycle_authority_root(
                visible_authority,
                "visible",
            )
            visible_reference = _publish_test_sandbox_reference(
                visible_authority,
                fixture,
            )
            visible_context = _model_context(
                supported=False,
                sandbox_reference=visible_reference,
            )
            visible_context["experiment_authority_root"] = str(
                visible_authority
            )
            visible = ModelInvocationCall(
                visible_lifecycle_root,
                visible_context,
                supported=False,
            )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "authority root is provider-visible",
            ):
                visible.execute(
                    [str(Path(sys.executable).resolve()), "-c", "pass"],
                    cwd=fixture["repository"],
                    input_text="",
                    timeout_seconds=10,
                )

    def test_sandbox_policy_survives_mailbox_context_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            reference = _publish_test_sandbox_reference(
                tmp,
                fixture,
            )
            message = {
                "payload": {
                    "model_invocation_context": {
                        "experiment_sandbox_reference": reference,
                        "experiment_sandbox_required": True,
                        "experiment_authority_root": tmp,
                        "experiment_controller_reference": {
                            "schema_version": (
                                "experiment_budget_controller_reference.v1"
                            )
                        },
                        "experiment_controller_required": True,
                    }
                }
            }
            projected = _model_invocation_context_payload(message)
            context = invocation_context_from_message(
                {"payload": projected},
                backend="codex",
            )
            self.assertTrue(context["experiment_sandbox_required"])
            self.assertEqual(
                context["experiment_sandbox_reference"],
                reference,
            )
            self.assertEqual(context["experiment_authority_root"], tmp)
            self.assertTrue(context["experiment_controller_required"])
            self.assertEqual(
                context["experiment_controller_reference"],
                {
                    "schema_version": (
                        "experiment_budget_controller_reference.v1"
                    )
                },
            )

    def test_trusted_argv_evaluation_waits_for_terminal_and_avoids_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner_patch = patch(
                "agentteam_runtime.experiment_sandbox._run_bounded_argv",
                side_effect=_test_evaluator_execution,
            )
            runner_patch.start()
            self.addCleanup(runner_patch.stop)
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            sandbox_reference = _publish_test_sandbox_reference(
                root,
                fixture,
            )
            lifecycle_authority_root = (
                experiment_lifecycle_authority_root(
                    root,
                    "fixture",
                )
            )
            lifecycle_context = _model_context(
                supported=False,
                sandbox_reference=None,
            )
            lifecycle_context["experiment_sandbox_policy_sha256"] = fixture[
                "descriptor"
            ]["policy_sha256"]
            lifecycle_context["experiment_sandbox_reference_sha256"] = (
                sandbox_reference["sha256"]
            )
            lifecycle = InvocationLifecycle(
                lifecycle_authority_root,
                lifecycle_context,
                invocation_id="INV-FIXTURE",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            evaluator = root / "trusted-evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\n"
                "import sys\n"
                "if len(sys.argv) < 3 or sys.argv[1] != '--':\n"
                "    raise SystemExit(64)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            evaluator_reference = publish_evaluator_reference(root, evaluator)
            evaluator_digest = evaluator_reference["sha256"]
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(91)\n",
                encoding="utf-8",
            )
            prompt = root / "prompt.txt"
            context = root / "context.json"
            taskpack = root / "taskpack"
            artifacts = root / "artifacts"
            prompt.write_text("safe prompt\n", encoding="utf-8")
            context.write_text("{}\n", encoding="utf-8")
            taskpack.mkdir()
            artifacts.mkdir()
            (taskpack / "task.json").write_text("{}\n", encoding="utf-8")
            scan_groups = {
                "prompt": [prompt],
                "context": [context],
                "taskpack": [taskpack],
                "artifacts": [artifacts],
            }
            scan_scope_reference = publish_scan_scope_reference(
                root,
                scan_groups,
            )
            invocation_set_reference = (
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": ["INV-FIXTURE"],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                )
            )
            marker = root / "shell-must-not-run"
            command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "import sys; print(sys.argv[1])",
                f"literal;touch {marker}",
            ]
            protocol = _sandbox_protocol(fixture)
            protocol["acceptance"] = {
                "command": command,
                "timeout_seconds": 10,
            }
            protocol["evaluator"]["artifact_sha256"] = evaluator_digest
            protocol_reference = publish_experiment_protocol_reference(
                root,
                protocol,
                reference_id="main-protocol",
            )

            with patch(
                "agentteam_runtime.experiment_sandbox.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ExperimentEvaluationBlocked,
                    "must terminate",
                ):
                    run_trusted_argv_evaluator(
                        authority_root=root,
                        invocation_set_reference=invocation_set_reference,
                        provider_sandbox_reference=sandbox_reference,
                        experiment_protocol_reference=protocol_reference,
                        scan_scope_reference=scan_scope_reference,
                        command=command,
                        cwd=fixture["repository"],
                        evaluator_reference=evaluator_reference,
                        canary_path=fixture["canary"],
                        timeout_seconds=10,
                    )
            popen.assert_not_called()

            lifecycle.finalize(
                "completed",
                stdout="",
                stderr="",
            )
            evidence_path = root / "evaluation.json"
            evidence = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=command,
                cwd=fixture["repository"],
                evaluator_reference=evaluator_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
                evidence_path=evidence_path,
            )

            self.assertEqual(
                evidence["evaluation_status"],
                "passed",
                evidence,
            )
            self.assertTrue(evidence["promotion_eligible"])
            self.assertTrue(evidence["evaluator_started"])
            self.assertFalse(marker.exists())
            self.assertEqual(len(evidence["terminal_invocations"]), 1)
            self.assertEqual(
                evidence["provider_sandbox_policy_sha256"],
                fixture["descriptor"]["policy_sha256"],
            )
            self.assertEqual(
                evidence["evaluator_artifact"],
                evaluator_reference["path"],
            )
            self.assertRegex(evidence["environment_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn(
                "AGENTTEAM_CREDENTIAL_FILE",
                "\0".join(evidence["argv"]),
            )
            self.assertNotIn(
                str(fixture["credential"]),
                "\0".join(evidence["argv"]),
            )
            self.assertEqual(
                evidence["run_id"],
                "RUN-EXPERIMENT-FIXTURE",
            )
            self.assertEqual(evidence["taskpack_ids"], ["phase2-fixture"])
            self.assertEqual(
                evidence["expected_invocation_ids"],
                ["INV-FIXTURE"],
            )
            self.assertEqual(evidence["pre_run_leak_scan"]["scan_status"], "clean")
            self.assertEqual(evidence["post_run_leak_scan"]["scan_status"], "clean")
            self.assertEqual(
                validate_evaluation_evidence(
                    evidence,
                    expected_run_id="RUN-EXPERIMENT-FIXTURE",
                    expected_taskpack_ids=["phase2-fixture"],
                    expected_protocol_sha256=evidence[
                        "experiment_protocol_sha256"
                    ],
                    expected_protocol_reference_sha256=(
                        protocol_reference["sha256"]
                    ),
                    expected_acceptance_command_sha256=evidence[
                        "acceptance_command_sha256"
                    ],
                    expected_acceptance_executable_sha256=evidence[
                        "acceptance_executable_sha256"
                    ],
                    expected_evaluator_sha256=evaluator_digest,
                    expected_invocation_set_reference_sha256=(
                        invocation_set_reference["sha256"]
                    ),
                    expected_provider_sandbox_reference_sha256=(
                        sandbox_reference["sha256"]
                    ),
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    experiment_protocol_reference=protocol_reference,
                    provider_sandbox_reference=sandbox_reference,
                    canary_path=fixture["canary"],
                ),
                evidence,
            )
            self.assertTrue(evidence_path.is_file())
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "run_id binding mismatch",
            ):
                validate_evaluation_evidence(
                    evidence,
                    expected_run_id="RUN-OTHER",
                )
            forged_evidence = copy.deepcopy(evidence)
            forged_evidence["expected_invocation_ids"] = ["INV-FORGED"]
            forged_evidence["invocation_sets"][0][
                "expected_invocation_ids"
            ] = ["INV-FORGED"]
            forged_evidence["terminal_invocations"][0]["invocation_id"] = (
                "INV-FORGED"
            )
            forged_evidence["invocation_set_seal_sha256"] = hashlib.sha256(
                json.dumps(
                    forged_evidence["invocation_sets"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "invocation authority binding mismatch",
            ):
                validate_evaluation_evidence(
                    forged_evidence,
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                )
            forged_candidate = copy.deepcopy(evidence)
            forged_candidate["provider_sandbox_reference_sha256"] = "f" * 64
            forged_candidate["pre_run_leak_scan"]["canary_sha256"] = "e" * 64
            forged_candidate["post_run_leak_scan"]["canary_sha256"] = "e" * 64
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "candidate sandbox binding mismatch",
            ):
                validate_evaluation_evidence(
                    forged_candidate,
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    experiment_protocol_reference=protocol_reference,
                    provider_sandbox_reference=sandbox_reference,
                    canary_path=fixture["canary"],
                )

            candidate_probe_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "from pathlib import Path\n"
                    "p=Path('../evaluator-only/gold-canary')\n"
                    "try:\n"
                    " p.read_bytes()\n"
                    "except OSError:\n"
                    " raise SystemExit(0)\n"
                    "raise SystemExit(91)\n"
                ),
            ]
            candidate_probe_protocol = copy.deepcopy(protocol)
            candidate_probe_protocol["acceptance"]["command"] = (
                candidate_probe_command
            )
            candidate_probe_reference = publish_experiment_protocol_reference(
                root,
                candidate_probe_protocol,
                reference_id="candidate-canary-probe",
            )
            use_real_candidate_boundary = (
                os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") == "1"
                and os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR")
                == "1"
            )
            if use_real_candidate_boundary:
                runner_patch.stop()
            try:
                candidate_probe_evidence = run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=candidate_probe_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=candidate_probe_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            finally:
                if use_real_candidate_boundary:
                    runner_patch.start()
            self.assertEqual(
                candidate_probe_evidence["evaluation_status"],
                "passed",
                candidate_probe_evidence,
            )

            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "preregistered acceptance command",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=[
                        str(Path(sys.executable).resolve()),
                        "-c",
                        "print('different evaluator')",
                    ],
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            different = root / "different-evaluator.py"
            different.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            different.chmod(0o700)
            different_reference = publish_evaluator_reference(
                root,
                different,
                reference_id="different-evaluator",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "trusted evaluator digest mismatch",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=command,
                    cwd=fixture["repository"],
                    evaluator_reference=different_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            shell_protocol = copy.deepcopy(protocol)
            shell_command = ["/bin/bash", "-c", "true"]
            shell_protocol["acceptance"]["command"] = shell_command
            shell_protocol_reference = publish_experiment_protocol_reference(
                root,
                shell_protocol,
                reference_id="shell-protocol",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "executable is not (approved|uniquely mapped)",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=shell_protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=shell_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            ignoring_evaluator = root / "ignoring-evaluator.py"
            ignoring_evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            ignoring_evaluator.chmod(0o700)
            ignoring_reference = publish_evaluator_reference(
                root,
                ignoring_evaluator,
                reference_id="ignoring-evaluator",
            )
            failing_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "raise SystemExit(97)",
            ]
            failing_protocol = _sandbox_protocol(fixture)
            failing_protocol["acceptance"] = {
                "command": failing_command,
                "timeout_seconds": 10,
            }
            failing_protocol["evaluator"]["artifact_sha256"] = (
                ignoring_reference["sha256"]
            )
            failing_protocol_reference = (
                publish_experiment_protocol_reference(
                    root,
                    failing_protocol,
                    reference_id="ignored-acceptance-protocol",
                )
            )
            ignored_acceptance = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=failing_protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=failing_command,
                cwd=fixture["repository"],
                evaluator_reference=ignoring_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
            )
            self.assertEqual(ignored_acceptance["evaluation_status"], "failed")
            self.assertEqual(ignored_acceptance["returncode"], 97)
            self.assertFalse(ignored_acceptance["promotion_eligible"])

            fake_python = root / "python3"
            fake_python.write_text(
                "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            fake_command = [str(fake_python), "-c", "print('unsafe')"]
            fake_protocol = copy.deepcopy(protocol)
            fake_protocol["acceptance"]["command"] = fake_command
            fake_protocol_reference = publish_experiment_protocol_reference(
                root,
                fake_protocol,
                reference_id="fake-python-protocol",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "executable is not (approved|uniquely mapped)",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=fake_protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=fake_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            truncated_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "import sys; sys.stdout.write('A' * 64)",
            ]
            truncated_protocol = _sandbox_protocol(fixture)
            truncated_protocol["acceptance"] = {
                "command": truncated_command,
                "timeout_seconds": 10,
            }
            truncated_protocol["evaluator"]["artifact_sha256"] = (
                evaluator_digest
            )
            truncated_protocol_reference = (
                publish_experiment_protocol_reference(
                    root,
                    truncated_protocol,
                    reference_id="truncated-protocol",
                )
            )
            truncated = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=truncated_protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=truncated_command,
                cwd=fixture["repository"],
                evaluator_reference=evaluator_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
                max_output_bytes=8,
            )
            self.assertEqual(truncated["evaluation_status"], "failed")
            self.assertFalse(truncated["promotion_eligible"])
            self.assertTrue(truncated["stdout_truncated"])
            self.assertEqual(
                truncated["failure_reason"],
                "evaluator_output_truncated",
            )

            valid_terminal = lifecycle.terminal_path.read_text(encoding="utf-8")
            terminal = json.loads(valid_terminal)
            terminal["lifecycle_owner_token"] = "OWNER-TAMPERED"
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "does not bind its start and sandbox",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            lifecycle.terminal_path.write_text(valid_terminal, encoding="utf-8")

            terminal = json.loads(valid_terminal)
            terminal.pop("experiment_sandbox_policy_sha256", None)
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "does not bind its start and sandbox",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            lifecycle.terminal_path.write_text(valid_terminal, encoding="utf-8")

            terminal = json.loads(
                lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            terminal["terminal_status"] = "running"
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "model invocation terminal schema failed",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

    def test_precreated_invocation_cannot_publish_after_evaluation_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            sandbox_reference = _publish_test_sandbox_reference(root, fixture)
            lifecycle_authority_root = (
                experiment_lifecycle_authority_root(
                    root,
                    "completed",
                )
            )
            context = _model_context(supported=False, sandbox_reference=None)
            context["experiment_sandbox_policy_sha256"] = fixture[
                "descriptor"
            ]["policy_sha256"]
            context["experiment_sandbox_reference_sha256"] = (
                sandbox_reference["sha256"]
            )
            completed = InvocationLifecycle(
                lifecycle_authority_root,
                context,
                invocation_id="INV-COMPLETED",
            )
            completed.publish_start(ExecutionGroupIdentity.not_applicable())
            completed.finalize("completed", stdout="", stderr="")
            late = InvocationLifecycle(
                lifecycle_authority_root,
                context,
                invocation_id="INV-LATE",
            )
            scan_paths = {}
            for group in ("prompt", "context", "taskpack", "artifacts"):
                path = root / f"{group}.txt"
                path.write_text("{}\n", encoding="utf-8")
                scan_paths[group] = [path]
            scan_reference = publish_scan_scope_reference(root, scan_paths)
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            evaluator_reference = publish_evaluator_reference(root, evaluator)
            shadow_authority = lifecycle_authority_root.parent / "shadow"
            shadow_authority.mkdir()
            shutil.copytree(
                completed.invocation_dir,
                shadow_authority / "model_invocations" / "INV-COMPLETED",
                dirs_exist_ok=True,
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "does not cover the lifecycle registry",
            ):
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": [
                                "INV-COMPLETED",
                                "INV-LATE",
                            ],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                    reference_id="shadow-manifest",
                )
            shutil.rmtree(shadow_authority)
            hidden_authority = root / "temporarily-hidden-authority"
            lifecycle_authority_root.rename(hidden_authority)
            try:
                with self.assertRaisesRegex(
                    ExperimentSandboxError,
                    "lifecycle authority.*unavailable",
                ):
                    publish_model_invocation_set_reference(
                        root,
                        "RUN-EXPERIMENT-FIXTURE",
                        [
                            {
                                "lifecycle_authority_root": (
                                    lifecycle_authority_root
                                ),
                                "taskpack_id": "phase2-fixture",
                                "invocation_ids": [
                                    "INV-COMPLETED",
                                    "INV-LATE",
                                ],
                                "sandbox_reference": sandbox_reference,
                            }
                        ],
                        reference_id="deleted-root-manifest",
                    )
            finally:
                hidden_authority.rename(lifecycle_authority_root)
            invocation_set_reference = (
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": [
                                "INV-COMPLETED",
                                "INV-LATE",
                            ],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                )
            )
            protocol = _sandbox_protocol(fixture)
            protocol["acceptance"]["command"] = [
                str(Path(sys.executable).resolve()),
                "-c",
                "raise SystemExit(0)",
            ]
            protocol["acceptance"]["timeout_seconds"] = 10
            protocol["evaluator"]["artifact_sha256"] = evaluator_reference[
                "sha256"
            ]
            protocol_reference = publish_experiment_protocol_reference(
                root,
                protocol,
            )

            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "durable start",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_reference,
                    command=protocol["acceptance"]["command"],
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "invocation set is sealed",
            ):
                late.publish_start(ExecutionGroupIdentity.not_applicable())

    def test_evaluation_seals_multiple_taskpack_authority_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "agentteam_runtime.experiment_sandbox._run_bounded_argv",
                side_effect=_test_evaluator_execution,
            ):
                root = Path(tmp)
                fixture = _sandbox_fixture(root)
                worker_sandbox_reference = (
                    _publish_test_sandbox_reference(
                        root,
                        fixture,
                    )
                )
                author_repository = root / "author-repository"
                shutil.copytree(
                    fixture["repository"],
                    author_repository,
                )
                author_identity = {
                    "commit": _git(
                        author_repository,
                        "rev-parse",
                        "HEAD",
                    ).stdout.strip(),
                    "tree": _git(
                        author_repository,
                        "rev-parse",
                        "HEAD^{tree}",
                    ).stdout.strip(),
                    "git_object_format": _git(
                        author_repository,
                        "rev-parse",
                        "--show-object-format",
                    ).stdout.strip(),
                }
                author_descriptor = build_provider_sandbox_descriptor(
                    author_repository,
                    runtime_views=[
                        {
                            "source": view["source"],
                            "target": view["target"],
                        }
                        for view in fixture["descriptor"]["runtime_views"]
                    ],
                    bwrap_path="/usr/bin/bwrap",
                    repository_target="/workspace-author",
                    repository_identity=author_identity,
                    forbidden_paths=[fixture["canary"]],
                )
                author_evidence = dict(fixture["evidence"])
                author_evidence["policy_sha256"] = author_descriptor[
                    "policy_sha256"
                ]
                with patch(
                    "agentteam_runtime.experiment_sandbox."
                    "probe_gold_canary_denial",
                    return_value=author_evidence,
                ):
                    author_sandbox_reference = (
                        publish_provider_sandbox_reference(
                            root,
                            author_descriptor,
                            fixture["canary"],
                            reference_id="author-sandbox",
                        )
                    )
                author_authority = experiment_lifecycle_authority_root(
                    root,
                    "author-context",
                )
                cross_context = _model_context(
                    supported=False,
                    sandbox_reference=author_sandbox_reference,
                )
                cross_context["experiment_sandbox_required"] = True
                cross_context["experiment_authority_root"] = str(root)
                cross_invocation = ModelInvocationCall(
                    author_authority,
                    cross_context,
                    supported=False,
                )
                with self.assertRaisesRegex(
                    ModelInvocationIntegrityError,
                    "cwd must remain inside",
                ):
                    cross_invocation.execute(
                        [str(Path(sys.executable).resolve()), "-c", "pass"],
                        cwd=fixture["repository"],
                        input_text="",
                        timeout_seconds=10,
                    )
                shutil.rmtree(cross_invocation.lifecycle.invocation_dir)
                worker_authority = experiment_lifecycle_authority_root(
                    root,
                    "worker-output",
                )
                authorities = {
                    "author-context": author_authority,
                    "worker-output": worker_authority,
                }
                invocation_sets = []
                for (
                    authority_name,
                    taskpack_id,
                    sandbox_reference,
                    sandbox_policy_sha256,
                    workspace,
                ) in (
                    (
                        "author-context",
                        "taskpack-authoring",
                        author_sandbox_reference,
                        author_descriptor["policy_sha256"],
                        author_repository,
                    ),
                    (
                        "worker-output",
                        "taskpack-implementation",
                        worker_sandbox_reference,
                        fixture["descriptor"]["policy_sha256"],
                        fixture["repository"],
                    ),
                ):
                    authority = authorities[authority_name]
                    context = _model_context(
                        supported=False,
                        sandbox_reference=sandbox_reference,
                    )
                    context["taskpack_id"] = taskpack_id
                    context["experiment_sandbox_required"] = True
                    context["experiment_authority_root"] = str(root)
                    invocation = ModelInvocationCall(
                        authority,
                        context,
                        supported=False,
                    )
                    with patch(
                        "agentteam_runtime.model_invocation."
                        "_run_bounded_process",
                        return_value=ProviderExecution([], 0, "", ""),
                    ):
                        invocation.execute(
                            [
                                str(Path(sys.executable).resolve()),
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            cwd=workspace,
                            input_text="",
                            timeout_seconds=10,
                        )
                    invocation.lifecycle.finalize(
                        "completed",
                        stdout="",
                        stderr="",
                    )
                    self.assertEqual(
                        invocation.lifecycle.context[
                            "experiment_sandbox_policy_sha256"
                        ],
                        sandbox_policy_sha256,
                    )
                    invocation_id = invocation.lifecycle.invocation_id
                    invocation_sets.append(
                        {
                            "lifecycle_authority_root": authority,
                            "taskpack_id": taskpack_id,
                            "invocation_ids": [invocation_id],
                            "sandbox_reference": sandbox_reference,
                        }
                    )
                sandbox_reference = worker_sandbox_reference
                scan_groups = {}
                for group in ("prompt", "context", "taskpack", "artifacts"):
                    path = root / f"{group}.json"
                    path.write_text("{}\n", encoding="utf-8")
                    scan_groups[group] = [path]
                scan_reference = publish_scan_scope_reference(
                    root,
                    scan_groups,
                )
                invocation_reference = (
                    publish_model_invocation_set_reference(
                        root,
                        "RUN-EXPERIMENT-FIXTURE",
                        invocation_sets,
                    )
                )
                evaluator = root / "multi-root-evaluator.py"
                evaluator.write_text(
                    "#!/usr/bin/python3\n"
                    "import sys\n"
                    "if len(sys.argv) < 3 or sys.argv[1] != '--':\n"
                    "    raise SystemExit(64)\n",
                    encoding="utf-8",
                )
                evaluator.chmod(0o700)
                evaluator_reference = publish_evaluator_reference(
                    root,
                    evaluator,
                )
                protocol = _sandbox_protocol(fixture)
                command = [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    "raise SystemExit(0)",
                ]
                protocol["acceptance"] = {
                    "command": command,
                    "timeout_seconds": 10,
                }
                protocol["evaluator"]["artifact_sha256"] = (
                    evaluator_reference["sha256"]
                )
                protocol_reference = publish_experiment_protocol_reference(
                    root,
                    protocol,
                )

                evidence = run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_reference,
                    command=command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            self.assertEqual(
                evidence["evaluation_status"],
                "passed",
                evidence,
            )
            self.assertEqual(
                evidence["taskpack_ids"],
                ["taskpack-authoring", "taskpack-implementation"],
            )
            self.assertEqual(
                evidence["expected_invocation_ids"],
                sorted(
                    invocation_id
                    for item in invocation_sets
                    for invocation_id in item["invocation_ids"]
                ),
            )
            self.assertEqual(len(evidence["invocation_sets"]), 2)
            self.assertEqual(len(evidence["terminal_invocations"]), 2)
            self.assertEqual(
                {
                    item["sandbox_policy_sha256"]
                    for item in evidence["invocation_sets"]
                },
                {
                    author_descriptor["policy_sha256"],
                    fixture["descriptor"]["policy_sha256"],
                },
            )

    def test_prompt_context_taskpack_and_artifact_canary_scans_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            root = Path(tmp)
            group_paths = {}
            for group in ("prompt", "context", "taskpack", "artifacts"):
                path = root / f"{group}.txt"
                path.write_text(f"safe {group}\n", encoding="utf-8")
                group_paths[group] = [path]
            clean = scan_canary_leakage(
                group_paths,
                canary_path=fixture["canary"],
            )
            self.assertEqual(clean["scan_status"], "clean")

            digest = hashlib.sha256(fixture["canary"].read_bytes()).hexdigest()
            Path(group_paths["context"][0]).write_text(
                f"context leaked {digest}\n",
                encoding="utf-8",
            )
            leaked = scan_canary_leakage(
                group_paths,
                canary_path=fixture["canary"],
            )
            self.assertEqual(leaked["scan_status"], "leak_detected")
            self.assertEqual(leaked["findings"][0]["group"], "context")
            self.assertEqual(leaked["findings"][0]["match"], "canary_sha256")

            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "byte bound",
            ):
                scan_canary_leakage(
                    group_paths,
                    canary_path=fixture["canary"],
                    max_bytes=1,
                )
            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "entry bound",
            ):
                scan_canary_leakage(
                    group_paths,
                    canary_path=fixture["canary"],
                    max_files=3,
                )


class ExperimentCalibrationTests(unittest.TestCase):
    FIXTURES = (
        Path(__file__).parent
        / "fixtures"
        / "phase2_experiments"
    )

    @staticmethod
    def _bind_operator_event(
        controller,
        protocol,
        manifest,
        run_dir,
        *,
        request_source,
    ):
        run_dir = Path(run_dir)
        state_path = (
            run_dir / "state" / "two_phase_scheduler_state.json"
        )
        state_path.parent.mkdir()
        state_path.write_text(
            json.dumps(
                {
                    "experiment_controller_reference": (
                        controller.reference
                    ),
                    "experiment_run_id": manifest[
                        "experiment_run_id"
                    ],
                    "experiment_run_manifest_sha256": (
                        canonical_json_sha256(manifest)
                    ),
                    "experiment_target_path_sha256": hashlib.sha256(
                        str(run_dir.resolve()).encode("utf-8")
                    ).hexdigest(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        payload = (
            {"request_id": "PERMISSION-CALIBRATION"}
            if request_source == "permission_request"
            else {"reason": "controlled calibration stop"}
        )
        event_type = (
            "permission_request_required"
            if request_source == "permission_request"
            else "run_stop_requested"
        )
        request = ExperimentOperatorActionLedgerTests._event(
            event_type,
            "2026-07-27T00:00:00Z",
            payload,
        )
        (run_dir / "events.jsonl").write_text(
            json.dumps(request, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        response = None
        answered_at = None
        if request_source == "permission_request":
            response = ExperimentOperatorActionLedgerTests._event(
                "permission_request_resolved",
                "2026-07-27T00:00:01Z",
                {
                    "request_id": "PERMISSION-CALIBRATION",
                    "decision": "approved",
                },
            )
            answered_at = "2026-07-27T00:00:01Z"
        controller.record_operator_action(
            protocol=protocol,
            run_manifest=manifest,
            run_dir=run_dir,
            request_source=request_source,
            request=request,
            response=response,
            requested_at="2026-07-27T00:00:00Z",
            answered_at=answered_at,
        )

    def _real_calibration_fixture(self, root):
        root = Path(root)
        projection_root = root / "projection"
        projection_root.mkdir()
        repository_root = root / "source"
        repository_root.mkdir()
        source = repository_root / "source"
        source.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(source)],
            check=True,
        )
        _git(source, "config", "user.name", "Calibration Fixture")
        _git(
            source,
            "config",
            "user.email",
            "calibration@example.invalid",
        )
        for fixture_name in ("deterministic_l1", "bounded_l2"):
            shutil.copytree(
                self.FIXTURES / fixture_name,
                source / fixture_name,
            )
        (source / "verify_fixtures.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import subprocess\n"
            "import sys\n"
            "root = Path(__file__).resolve().parent\n"
            "for name in ('deterministic_l1', 'bounded_l2'):\n"
            "    fixture = root / name\n"
            "    env = dict(os.environ, PYTHONPATH=str(fixture))\n"
            "    result = subprocess.run(\n"
            "        [sys.executable, '-B', '-m', 'unittest', 'discover', "
            "'-s', 'tests'],\n"
            "        cwd=fixture,\n"
            "        env=env,\n"
            "        check=False,\n"
            "    )\n"
            "    if result.returncode:\n"
            "        raise SystemExit(result.returncode)\n",
            encoding="utf-8",
        )
        _git(source, "add", ".")
        _git(source, "commit", "--quiet", "-m", "calibration fixtures")
        repository = {
            "repository": {
                "source": str(source),
                "commit": _git(
                    source,
                    "rev-parse",
                    "HEAD",
                ).stdout.strip(),
                "tree": _git(
                    source,
                    "rev-parse",
                    "HEAD^{tree}",
                ).stdout.strip(),
                "git_object_format": _git(
                    source,
                    "rev-parse",
                    "--show-object-format",
                ).stdout.strip(),
            },
            "source": source,
        }
        runtime_release = _filesystem_release(
            root / "candidate-runtime",
            repository["repository"]["commit"],
        )
        taskpack_draft = draft_taskpack_files(
            project_root=repository["source"],
            goal="Repair both deterministic calibration fixtures.",
            draft_root=root / "taskpack-draft",
            taskpack_id="phase2-calibration-direct",
            read_scope=[
                "deterministic_l1",
                "bounded_l2",
                "verify_fixtures.py",
            ],
            write_scope=[
                "deterministic_l1/src/counter.py",
                "bounded_l2/src/records.py",
            ],
            verification_command=[
                str(Path(sys.executable).resolve()),
                "-B",
                "verify_fixtures.py",
            ],
            role_routing=False,
        )
        frozen = freeze_taskpack(
            taskpack_draft["taskpack_dir"],
            root / "taskpack-frozen",
            expected_authoring_mode="direct_draft",
        )
        evaluator = root / "calibration-evaluator.py"
        evaluator.write_text(
            "#!/usr/bin/python3\n"
            "import subprocess\n"
            "import sys\n"
            "if len(sys.argv) < 3 or sys.argv[1] != '--':\n"
            "    raise SystemExit(64)\n"
            "raise SystemExit(subprocess.run(sys.argv[2:]).returncode)\n",
            encoding="utf-8",
        )
        evaluator.chmod(0o700)
        protocol = copy.deepcopy(_protocol())
        protocol["repository"] = repository["repository"]
        protocol["acceptance"] = {
            "command": [
                str(Path(sys.executable).resolve()),
                "-B",
                "verify_fixtures.py",
            ],
            "timeout_seconds": 30,
        }
        protocol["evaluator"]["artifact_sha256"] = hashlib.sha256(
            evaluator.read_bytes()
        ).hexdigest()
        protocol["direct_taskpack"]["sha256"] = frozen["manifest"][
            "digest_sha256"
        ]
        protocol["repetition_policy"]["count"] = 3
        protocol["budgets"]["max_total_tokens"] = 28
        controller = create_experiment_controller(
            projection_root / "protocol-controller",
            protocol_id=protocol["experiment_id"],
            max_total_tokens=protocol["budgets"][
                "max_total_tokens"
            ],
            max_wall_time_seconds=protocol["budgets"][
                "max_wall_time_seconds"
            ],
            soft_warning_ratio=protocol["budgets"][
                "soft_warning_ratio"
            ],
            scored=protocol["scored"],
            protocol_sha256=canonical_json_sha256(protocol),
            operator_limits=protocol["operator_limits"],
        )
        credential = projection_root / "credential.json"
        credential.write_text("{}\n", encoding="utf-8")
        canary = projection_root / "gold-canary"
        canary.write_text(
            "phase2 real calibration canary\n",
            encoding="utf-8",
        )
        sandbox_configuration = {
            "runtime_views": [
                {"source": path, "target": path}
                for path in ("/usr", "/lib", "/lib64", "/bin")
                if Path(path).exists()
            ],
            "library_views": [],
            "credential_mounts": [
                {
                    "source": str(credential),
                    "target": (
                        "/run/agentteam-credentials/provider.json"
                    ),
                }
            ],
            "environment": {
                "AGENTTEAM_CREDENTIAL_FILE": (
                    "/run/agentteam-credentials/provider.json"
                )
            },
            "canary_path": str(canary),
        }
        execution = {
            "single_returncode": 0,
            "forced_runtime_status": None,
        }

        def apply_fixture_change(workspace):
            workspace = Path(workspace)
            (
                workspace
                / "deterministic_l1"
                / "src"
                / "counter.py"
            ).write_text(
                "def increment(value):\n"
                "    return value + 1\n",
                encoding="utf-8",
            )
            (
                workspace
                / "bounded_l2"
                / "src"
                / "records.py"
            ).write_text(
                "def normalize(record):\n"
                "    return {\n"
                "        \"label\": record.get(\"label\") or \"unknown\",\n"
                "        \"value\": int(record[\"value\"]),\n"
                "    }\n",
                encoding="utf-8",
            )

        class CalibrationSingleRunner:
            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                from agentteam_runtime.experiment_sandbox import (
                    load_experiment_launch_registration,
                )

                registration = load_experiment_launch_registration(
                    lifecycle.authority_root
                )
                del input_text, timeout_seconds, environment
                if lifecycle.context["usage_stage"] == "taskpack_author":
                    authored = (
                        Path(registration["workspace_root"])
                        / ".agentteam-author"
                        / lifecycle.context["taskpack_id"]
                    )
                    if not authored.is_dir():
                        raise AssertionError(
                            "calibration author target is missing"
                        )
                    for source_file in Path(
                        taskpack_draft["taskpack_dir"]
                    ).iterdir():
                        shutil.copy2(
                            source_file,
                            authored / source_file.name,
                        )
                    taskpack_path = authored / "taskpack.yaml"
                    taskpack = json.loads(
                        taskpack_path.read_text(encoding="utf-8")
                    )
                    taskpack.update(
                        {
                            "taskpack_id": authored.name,
                            "project_root": registration[
                                "workspace_root"
                            ],
                        }
                    )
                    taskpack_path.write_text(
                        json.dumps(taskpack, sort_keys=True),
                        encoding="utf-8",
                    )
                else:
                    apply_fixture_change(
                        registration["workspace_root"]
                    )
                self.command = list(command)

            def prepare(self):
                return _supported_execution_identity()

            def permit_and_wait(self, **_kwargs):
                return ProviderExecution(
                    self.command,
                    execution["single_returncode"],
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 2,
                                "cached_input_tokens": 1,
                                "output_tokens": 1,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 3,
                            },
                        }
                    ),
                    "",
                )

            def abort_before_permit(self):
                return None

            def cleanup_after_terminal(self):
                return None

        class CalibrationWorkerRuntime:
            def run(
                self,
                message,
                worktree_path=None,
                progress_callback=None,
            ):
                del progress_callback
                apply_fixture_change(worktree_path)
                invocation_context = invocation_context_from_message(
                    message,
                    model=protocol["environment"]["model"],
                    backend="codex",
                )
                invocation_context[
                    "model_invocation_authority_root"
                ] = message["payload"][
                    "model_invocation_authority_root"
                ]
                invocation_id = _complete_fake_experiment_invocation(
                    invocation_context,
                    worktree_path,
                    role="implementation_worker",
                    execution_identity=_supported_execution_identity(),
                )
                changed_files = [
                    "bounded_l2/src/records.py",
                    "deterministic_l1/src/counter.py",
                ]
                return {
                    "result_status": "completed",
                    "changed_files": changed_files,
                    "output": {
                        "adapter": "calibration_worker",
                        "operator_summary": {
                            "what_changed": [
                                "已修复两个确定性校准样例。"
                            ],
                            "verification_summary": [
                                "冻结任务包中的验证命令已通过。"
                            ],
                            "deliverables": [
                                {
                                    "deliverable": deliverable,
                                    "summary": (
                                        "确定性校准已交付："
                                        f"{deliverable}。"
                                    ),
                                    "evidence": changed_files,
                                }
                                for deliverable in (
                                    "goal_alignment_summary",
                                    (
                                        "implemented_changes_or_"
                                        "no_safe_change_rationale"
                                    ),
                                    "verification_summary",
                                    "next_steps",
                                )
                            ],
                        },
                        "model_invocation_id": invocation_id,
                    },
                }

        class CalibrationWorkerPool:
            def __init__(
                self,
                agent_pool_path,
                output_dir,
                **_kwargs,
            ):
                self.agent_pool_path = Path(agent_pool_path)
                self.output_dir = Path(output_dir)
                self.processed = set()

            def start(self):
                return {"worker_pool_status": "started"}

            def stop(self):
                return {"worker_pool_status": "stopped"}

            def health_check(self):
                return {
                    "pool_status": "running",
                    "workers": [
                        {
                            "worker_agent_id": (
                                "calibration-implementation-worker"
                            ),
                            "worker_status": "running",
                        }
                    ],
                }

            def supervise_once(self):
                state_path = (
                    self.output_dir
                    / "state"
                    / "two_phase_scheduler_state.json"
                )
                if state_path.is_file():
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    for attempt in state.get(
                        "inflight_attempts",
                        [],
                    ):
                        if attempt["message_id"] in self.processed:
                            continue
                        worker = FileMailboxWorker(
                            self.agent_pool_path,
                            attempt["step_dir"],
                            attempt["agent_id"],
                            runtime_adapter=CalibrationWorkerRuntime(),
                        )
                        worker.poll_once(
                            message_id=attempt["message_id"],
                            worktree_path=attempt["worktree_path"],
                        )
                        self.processed.add(attempt["message_id"])
                health = self.health_check()
                return {
                    "supervision_status": health["pool_status"],
                    "restarted_count": 0,
                    "before": health,
                    "restart": {"restarted_count": 0},
                    "after": health,
                }

        def run_runtime_command(command, **_kwargs):
            requested_status = execution["forced_runtime_status"]
            if requested_status == "infrastructure_failed":
                raise OSError("controlled calibration runtime failure")
            output = io.StringIO()
            with patch.object(
                cli_module,
                "FileMailboxWorkerPoolSupervisor",
                CalibrationWorkerPool,
            ), redirect_stdout(output):
                cli_module.main(command[3:])
            scheduler_status = {
                "completed": "completed",
                "failed": "completed",
                "interrupted": "interrupted",
                "budget_stopped": "budget_stopped",
            }[requested_status]
            stdout = output.getvalue() + json.dumps(
                {"scheduler_status": scheduler_status},
                sort_keys=True,
            ) + "\n"
            return subprocess.CompletedProcess(
                command,
                1 if requested_status == "failed" else 0,
                stdout,
                "",
            )

        def run_bounded_argv_without_systemd(
            argv,
            *,
            cwd,
            environment,
            timeout_seconds,
            max_output_bytes,
            cpu_limit,
            memory_limit_bytes,
            input_bytes=None,
        ):
            del cpu_limit, memory_limit_bytes
            try:
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    env=environment,
                    input=input_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    check=False,
                )
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    argv,
                    -9,
                    exc.stdout or b"",
                    exc.stderr or b"",
                )
                timed_out = True
            if execution["forced_runtime_status"] in {
                "failed",
                "infrastructure_failed",
                "interrupted",
            }:
                completed = subprocess.CompletedProcess(
                    argv,
                    1,
                    completed.stdout,
                    completed.stderr,
                )
            stdout_bytes = bytes(completed.stdout or b"")[
                :max_output_bytes
            ]
            stderr_bytes = bytes(completed.stderr or b"")[
                :max_output_bytes
            ]
            return {
                "returncode": completed.returncode,
                "timed_out": timed_out,
                "stdout": stdout_bytes.decode(
                    "utf-8",
                    errors="replace",
                ),
                "stderr": stderr_bytes.decode(
                    "utf-8",
                    errors="replace",
                ),
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "stdout_truncated": len(completed.stdout or b"")
                > max_output_bytes,
                "stderr_truncated": len(completed.stderr or b"")
                > max_output_bytes,
                "execution_boundary": (
                    "systemd_user_transient_service"
                ),
                "systemd_unit": (
                    "agentteam-eval-000000000000000000000000.service"
                ),
            }

        results = []
        base_order = protocol["mode_order"]
        sequence = [
            (0, mode, "completed")
            for mode in base_order
        ]
        rotated = base_order[1:] + base_order[:1]
        sequence.extend(
            [
                (1, rotated[0], "infrastructure_failed"),
                (1, rotated[1], "completed"),
                (1, rotated[2], "failed"),
            ]
        )
        twice_rotated = base_order[2:] + base_order[:2]
        sequence.extend(
            [
                (2, twice_rotated[0], "failed"),
                (2, twice_rotated[1], "interrupted"),
                (2, twice_rotated[2], "budget_stopped"),
            ]
        )
        with patch(
            "agentteam_runtime.model_invocation."
            "SystemdGatedExecution",
            CalibrationSingleRunner,
        ), patch(
            "agentteam_runtime.agentteam."
            "_run_runtime_command_with_progress",
            side_effect=run_runtime_command,
        ), patch(
            "agentteam_runtime.experiment_sandbox."
            "_run_bounded_argv",
            side_effect=run_bounded_argv_without_systemd,
        ):
            for repetition, mode, status in sequence:
                key = (
                    f"real-calibration-r{repetition}-{mode}"
                )
                allocation = allocate_experiment_run(
                    projection_root,
                    protocol,
                    mode=mode,
                    repetition_index=repetition,
                    stable_request_key=key,
                    runtime_release=runtime_release,
                    bound_at="2026-07-27T00:00:00Z",
                )
                run_dir = Path(allocation["run_dir"])
                snapshot = allocate_clean_snapshot(
                    run_dir,
                    protocol["repository"],
                    attested_at="2026-07-27T00:00:00Z",
                )
                authority_root = run_dir / "authority"
                authority_root.mkdir()
                if repetition == 0 and mode == base_order[0]:
                    self._bind_operator_event(
                        controller,
                        protocol,
                        allocation["run_manifest"],
                        run_dir,
                        request_source="permission_request",
                    )
                if status == "infrastructure_failed":
                    self._bind_operator_event(
                        controller,
                        protocol,
                        allocation["run_manifest"],
                        run_dir,
                        request_source="run_stop",
                    )
                execution["single_returncode"] = (
                    1 if status == "failed" else 0
                )
                execution["forced_runtime_status"] = status
                if mode == "single_codex":
                    adapter = SingleCodexModeAdapter()
                elif mode == "agentteam_direct":
                    adapter = AgentTeamDirectModeAdapter(
                        frozen["frozen_taskpack_dir"]
                    )
                else:
                    adapter = AgentTeamFullModeAdapter()
                mode_controller = ExperimentModeController(
                    protocol=protocol,
                    run_manifest=allocation["run_manifest"],
                    run_dir=run_dir,
                    project_root=snapshot["snapshot_path"],
                    authority_root=authority_root,
                    controller_reference=controller.reference,
                    sandbox_configuration=sandbox_configuration,
                    common_finalizer=ExperimentCommonFinalizer(
                        evaluator_artifact=evaluator,
                        runtime_release_identity=runtime_release,
                    ),
                    runtime_release_identity=runtime_release,
                )
                mode_result = mode_controller.execute(adapter)
                results.append(
                    {
                        "run_dir": str(run_dir),
                        "sandbox_authority_root": str(
                            authority_root
                        ),
                        "canary_path": str(canary),
                        "manifest": allocation["run_manifest"],
                        "bundle_sha256": mode_result[
                            "sealed_result"
                        ]["bundle_sha256"],
                        "stable_request_key": key,
                        "status": status,
                    }
                )
        primary = [
            item
            for item in results
            if item["manifest"]["repetition_index"] == 0
        ]
        repeat = next(
            item
            for item in results
            if item["manifest"]["repetition_index"] == 1
            and item["status"] == "completed"
        )
        controlled = [
            item
            for item in results
            if item["manifest"]["repetition_index"] > 0
            and item["status"] != "completed"
        ]
        duplicate_source = primary[0]
        return {
            "projection_root": projection_root,
            "protocol": protocol,
            "release": runtime_release,
            "primary": primary,
            "repeat": repeat,
            "controlled": controlled,
            "duplicate": {
                "stable_request_key": duplicate_source[
                    "stable_request_key"
                ],
                "experiment_run_id": duplicate_source["manifest"][
                    "experiment_run_id"
                ],
                "result_bundle_sha256": duplicate_source[
                    "bundle_sha256"
                ],
            },
        }

    def test_deterministic_l1_l2_calibration_rebuilds_all_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._real_calibration_fixture(tmp)
            output_path = (
                fixture["projection_root"]
                / "calibration"
                / "phase2-deterministic.json"
            )
            result = run_deterministic_experiment_calibration(
                protocol=fixture["protocol"],
                projection_root=fixture["projection_root"],
                primary_runs=fixture["primary"],
                repeat_run=fixture["repeat"],
                controlled_runs=fixture["controlled"],
                duplicate_request_evidence=fixture["duplicate"],
                fixture_roots={
                    "deterministic_l1": (
                        self.FIXTURES / "deterministic_l1"
                    ),
                    "bounded_l2": self.FIXTURES / "bounded_l2",
                },
                output_path=output_path,
            )
            report = result["report"]
            self.assertEqual(report["calibration_status"], "passed")
            self.assertEqual(
                [item["mode"] for item in report["mode_results"]],
                [
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                ],
            )
            self.assertEqual(
                report["usage_coverage"]["lifecycle_percent"],
                100,
            )
            self.assertEqual(
                report["usage_coverage"]["token_percent"],
                100,
            )
            self.assertEqual(
                report["projection_rebuild"]["result_count"],
                9,
            )
            self.assertEqual(
                report["controlled_outcomes"]["retention_status"],
                "passed",
            )
            self.assertEqual(
                set(
                    report["controlled_outcomes"][
                        "terminal_statuses"
                    ]
                ),
                {
                    "budget_stopped",
                    "failed",
                    "infrastructure_failed",
                    "interrupted",
                },
            )
            self.assertFalse(
                report["readiness_promotion_candidate"][
                    "benchmark_superiority_claim"
                ]
            )
            loaded = load_deterministic_calibration_report(
                output_path
            )
            self.assertEqual(
                loaded["report_sha256"],
                result["report_sha256"],
            )
            replay = run_deterministic_experiment_calibration(
                protocol=fixture["protocol"],
                projection_root=fixture["projection_root"],
                primary_runs=fixture["primary"],
                repeat_run=fixture["repeat"],
                controlled_runs=fixture["controlled"],
                duplicate_request_evidence=fixture["duplicate"],
                fixture_roots={
                    "deterministic_l1": (
                        self.FIXTURES / "deterministic_l1"
                    ),
                    "bounded_l2": self.FIXTURES / "bounded_l2",
                },
                output_path=output_path,
            )
            self.assertFalse(replay["publication"]["created"])

    def test_calibration_rejects_duplicate_request_cost_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._real_calibration_fixture(tmp)
            fixture["duplicate"]["stable_request_key"] = (
                "forged-request-key"
            )
            with self.assertRaisesRegex(
                ExperimentCalibrationError,
                "duplicate request",
            ):
                run_deterministic_experiment_calibration(
                    protocol=fixture["protocol"],
                    projection_root=fixture["projection_root"],
                    primary_runs=fixture["primary"],
                    repeat_run=fixture["repeat"],
                    controlled_runs=fixture["controlled"],
                    duplicate_request_evidence=fixture["duplicate"],
                    fixture_roots={
                        "deterministic_l1": (
                            self.FIXTURES / "deterministic_l1"
                        ),
                        "bounded_l2": (
                            self.FIXTURES / "bounded_l2"
                        ),
                    },
                )


class Phase2GateTests(unittest.TestCase):
    SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
    REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
    CAPABILITY_TEST_IDS = {
        "invocation_level_real_usage": (
            "tests.test_phase1_usage_end_to_end."
            "Phase1UsageEndToEndTests."
            "test_positive_fixture_has_complete_exact_replay_stable_accounting",
        ),
        "three_mode_experiment_harness": (
            "tests.test_experiment_harness."
            "ExperimentContractSchemaTests."
            "test_protocol_requires_complete_three_mode_equal_input_contract",
            "tests.test_experiment_harness."
            "ExperimentModeAdapterTests."
            "test_counterbalanced_mode_order_is_enforced_and_immutable",
            "tests.test_experiment_harness."
            "ExperimentCalibrationTests."
            "test_deterministic_l1_l2_calibration_rebuilds_all_evidence",
        ),
        "immutable_experiment_manifest": (
            "tests.test_experiment_harness."
            "ExperimentContractSchemaTests."
            "test_run_manifest_binds_mode_repetition_request_and_protocol",
            "tests.test_experiment_harness."
            "ExperimentAllocationTests."
            "test_tampered_published_protocol_fails_closed_without_provider",
            "tests.test_experiment_harness."
            "ExperimentLeaseAndResumeTests."
            "test_resume_accepts_exact_binding_and_rejects_all_identity_drift",
        ),
        "clean_reset_and_blind_gold_isolation": (
            "tests.test_experiment_harness."
            "ExperimentWorkspaceTests."
            "test_allocates_independent_exact_commit_snapshot_and_attestation",
            "tests.test_experiment_harness."
            "ExperimentSandboxTests."
            "test_provider_environment_rejects_canary_content_and_digest",
        ),
        "actual_budget_enforcement": (
            "tests.test_experiment_harness."
            "ExperimentBudgetTests."
            "test_token_warning_and_exhaustion_include_exact_boundaries",
            "tests.test_experiment_harness."
            "ExperimentBudgetTests."
            "test_one_lane_terminal_completion_exposes_overshoot",
            "tests.test_experiment_harness."
            "ExperimentProviderBudgetBoundaryTests."
            "test_denied_prelaunch_creates_no_runner_start_or_process",
            "tests.test_experiment_harness."
            "ExperimentProviderBudgetBoundaryTests."
            "test_interruption_resume_preserves_original_remaining_budget",
            "tests.test_experiment_harness."
            "TwoPhaseSchedulerExperimentBoundaryTests."
            "test_scheduler_denies_worker_dispatch_after_budget_exhaustion",
            "tests.test_experiment_harness."
            "TwoPhaseSchedulerExperimentBoundaryTests."
            "test_preintegration_exhaustion_preserves_patch_and_baseline",
        ),
        "operator_action_ledger": (
            "tests.test_experiment_harness."
            "ExperimentOperatorActionLedgerTests."
            "test_supported_inputs_map_to_closed_vocabulary_without_raw_content",
            "tests.test_experiment_harness."
            "ExperimentOperatorActionLedgerTests."
            "test_replay_is_idempotent_and_conflicting_response_fails_closed",
            "tests.test_experiment_harness."
            "ExperimentOperatorActionLedgerTests."
            "test_experiment_gateways_account_before_applying_runtime_input",
            "tests.test_experiment_harness."
            "ExperimentOperatorActionLedgerTests."
            "test_experiment_stop_capability_validates_against_real_ledger",
        ),
        "machine_readable_result_bundle": (
            "tests.test_experiment_harness."
            "ExperimentResultBundleTests."
            "test_terminal_bundle_is_atomic_idempotent_and_conflict_safe",
            "tests.test_experiment_harness."
            "ExperimentResultBundleTests."
            "test_recovery_snapshots_are_versioned_and_separate",
            "tests.test_experiment_harness."
            "ExperimentResultBundleTests."
            "test_projection_rebuild_preserves_all_outcomes_and_digests",
        ),
    }

    def _action_input(self, gate_id):
        digest = "0" * 64
        binding = {
            "path": "/fixture/authority.json",
            "sha256": digest,
        }
        if gate_id == "P2-08":
            configuration = {
                "promoted_at": "2026-07-29T00:00:00Z",
                "readiness_reasons": {
                    capability_id: "Validated fixture evidence."
                    for capability_id in self.CAPABILITY_TEST_IDS
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
                        self.CAPABILITY_TEST_IDS.items()
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

    def _repository(self, root):
        repository = Path(root) / "repository"
        repository.mkdir()
        _git(repository, "init", "--quiet")
        _git(repository, "config", "user.name", "Phase 2 Gate Test")
        _git(
            repository,
            "config",
            "user.email",
            "phase2-gate@example.invalid",
        )
        return repository

    @staticmethod
    def _write_calibration_report(path, source_commit):
        report = {
            "schema_version": "phase2_deterministic_calibration.v1",
            "calibration_status": "passed",
            "claim_scope": (
                "experiment_harness_readiness_only_not_benchmark_evidence"
            ),
            "protocol_sha256": "1" * 64,
            "source_commit": source_commit,
            "fixture_evidence": {
                name: {
                    "fixture_sha256": digest * 64,
                    "file_count": 1,
                }
                for name, digest in (
                    ("deterministic_l1", "2"),
                    ("bounded_l2", "3"),
                )
            },
            "mode_results": [
                {"mode": mode}
                for mode in (
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                )
            ],
            "repeat_result": {"mode": "single_codex"},
            "repeat_drift": {
                "status": "complete",
                "mode": "single_codex",
                "acceptance_status_equal": True,
                "changed_files_equal": True,
                "superiority_interpretation": False,
            },
            "controlled_outcomes": {
                "controlled_failure_run_ids": [
                    "failed",
                    "infrastructure-failed",
                    "interrupted",
                ],
                "budget_stopped_run_ids": ["budget-stopped"],
                "terminal_statuses": [
                    "budget_stopped",
                    "failed",
                    "infrastructure_failed",
                    "interrupted",
                ],
                "retention_status": "passed",
            },
            "duplicate_request": {
                "status": "passed",
                "allocation_status": "existing",
                "provider_calls_during_duplicate_allocation": 0,
                "result_bundle_sha256": "4" * 64,
            },
            "usage_coverage": {
                "lifecycle_percent": 100,
                "token_percent": 100,
                "cached_input_distinct": True,
            },
            "isolation": {
                "clean_snapshot_status": "passed",
                "canary_denial_status": "passed",
                "retained_leak_scan_status": "passed",
            },
            "operator_ledger": {
                "status": "passed",
                "operator_action_counts": {
                    "expected_operator_action": 1,
                    "corrective_intervention": 1,
                    "decision_escalation": 1,
                },
            },
            "projection_rebuild": {"status": "passed"},
            "comparison": {
                "status": "complete",
                "result_count": 7,
                "sha256": "5" * 64,
                "retained_terminal_statuses": [
                    "budget_stopped",
                    "completed",
                    "failed",
                    "infrastructure_failed",
                    "interrupted",
                ],
            },
            "readiness_promotion_candidate": {
                "status": "eligible",
                "live_provider_calls_required": 0,
                "benchmark_superiority_claim": False,
            },
        }
        path.write_bytes(canonical_json_bytes(report) + b"\n")
        return report

    def test_gate_registry_rejects_incomplete_action_contract(self):
        declaration = {
            "gate_id": "P2-10",
            "executor": "deterministic_controller",
            "controller_entrypoint": (
                "phase2_finalization_controller_v1"
            ),
            "relation_validator": (
                "phase2_finalization_relation_v1"
            ),
            "evidence_schema": (
                "experiments/native_agentteam_runtime/schemas/"
                "phase2_finalization.schema.json"
            ),
            "operator_authorization_required": False,
            "controller_action_input": {
                "schema_version": "phase2_gate_action_input.v1",
                "action": "finalize_phase2",
                "configuration": {"fixture": True},
            },
        }
        with self.assertRaisesRegex(
            Phase2GateError,
            "configuration is incomplete",
        ):
            resolve_gate_spec(declaration)

    def test_gate_registry_rejects_unsafe_pilot_request_key(self):
        action_input = self._action_input("P2-08")
        action_input["configuration"][
            "pilot_stable_request_key"
        ] = "../unsafe"
        with self.assertRaisesRegex(
            Phase2GateError,
            "stable request key is invalid",
        ):
            resolve_gate_spec(
                {
                    "gate_id": "P2-08",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_readiness_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_readiness_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_readiness_promotion.schema.json"
                    ),
                    "operator_authorization_required": False,
                    "controller_action_input": action_input,
                }
            )

    def test_action_authority_rejects_symlink_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            artifact = outside / "artifact.json"
            artifact.write_text("{}\n", encoding="utf-8")
            (allowed / "escape").symlink_to(
                outside,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                Phase2GateError,
                "outside its declared authority roots",
            ):
                experiment_gates_module._action_authority_files(
                    {
                        "evaluator": {
                            "path": str(
                                allowed
                                / "escape"
                                / "artifact.json"
                            ),
                            "sha256": hashlib.sha256(
                                artifact.read_bytes()
                            ).hexdigest(),
                        }
                    },
                    required=("evaluator",),
                    authority_roots=[allowed],
                )

    def test_action_authority_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            artifact = actual / "artifact.json"
            artifact.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                Phase2GateError,
                "outside its declared authority roots",
            ):
                experiment_gates_module._action_authority_files(
                    {
                        "evaluator": {
                            "path": str(artifact),
                            "sha256": hashlib.sha256(
                                artifact.read_bytes()
                            ).hexdigest(),
                        }
                    },
                    required=("evaluator",),
                    authority_roots=[linked],
                )

    def _repository_clone(self, root):
        repository = Path(root) / "repository"
        _git(
            Path(root),
            "clone",
            "--quiet",
            "--no-hardlinks",
            str(self.REPOSITORY_ROOT),
            str(repository),
        )
        _git(repository, "config", "user.name", "Phase 2 Gate Test")
        _git(
            repository,
            "config",
            "user.email",
            "phase2-gate@example.invalid",
        )
        overlay = subprocess.run(
            [
                "git",
                "-C",
                str(self.REPOSITORY_ROOT),
                "diff",
                "--binary",
                "HEAD",
                "--",
                "experiments/native_agentteam_runtime",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        if overlay:
            applied = subprocess.run(
                ["git", "-C", str(repository), "apply", "--binary", "-"],
                input=overlay,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if applied.returncode != 0:
                raise AssertionError(
                    applied.stderr.decode("utf-8", errors="replace")
                )
            _git(repository, "add", "experiments/native_agentteam_runtime")
            _git(
                repository,
                "commit",
                "--quiet",
                "-m",
                "overlay Phase 2 test worktree",
            )
        return repository

    def test_live_authorization_is_epoch_bound_and_precedes_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            schema_dir = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
            )
            schema_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                self.SCHEMAS / "phase2_live_authorization.schema.json",
                schema_dir / "phase2_live_authorization.schema.json",
            )
            context = {
                "repository_root": str(repository),
                "epoch_number": 2,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": "2" * 64,
                "readiness_promotion_sha256": "3" * 64,
                "model": "gpt-5.6",
                "reasoning_profile": "high",
                "max_total_tokens": 5000,
                "max_wall_time_seconds": 600.0,
            }
            declaration = {
                "gate_id": "P2-09",
                "executor": "deterministic_controller",
                "controller_entrypoint": (
                    "phase2_live_calibration_controller_v1"
                ),
                "relation_validator": (
                    "phase2_live_calibration_relation_v1"
                ),
                "evidence_schema": (
                    "experiments/native_agentteam_runtime/schemas/"
                    "phase2_calibration.schema.json"
                ),
                "operator_authorization_required": True,
                "operator_authorization_schema": (
                    "experiments/native_agentteam_runtime/schemas/"
                    "phase2_live_authorization.schema.json"
                ),
                "controller_action_input": self._action_input("P2-09"),
            }
            spec = resolve_gate_spec(declaration)
            provider_launcher = Mock()
            missing = run_gate_controller(
                spec,
                root / "missing-calibration.json",
                context,
                result_path=root / "controller-result.json",
                provider_launcher=provider_launcher,
            )
            self.assertEqual(
                missing["controller_status"],
                "awaiting_operator_authorization",
            )
            self.assertEqual(missing["provider_calls"], 0)
            provider_launcher.assert_not_called()
            self.assertFalse((root / "controller-result.json").exists())

            authorization = {
                "schema_version": "phase2_live_authorization.v1",
                "decision": "approved",
                "operator_identity": "phase2-test",
                "authorized_at": "2026-07-26T00:00:00Z",
                "gate_id": "P2-09",
                "epoch_number": 2,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": "2" * 64,
                "readiness_promotion_sha256": "3" * 64,
                "model": "gpt-5.6",
                "reasoning_profile": "high",
                "max_total_tokens": 5000,
                "max_wall_time_seconds": 600.0,
                "max_inflight_model_invocations": 1,
                "modes": [
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                ],
            }
            authorization_path = root / "authorization.json"
            first = publish_live_authorization(
                authorization_path,
                authorization,
                context,
            )
            second = publish_live_authorization(
                authorization_path,
                authorization,
                context,
            )
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["sha256"], second["sha256"])
            permitted = require_live_provider_authorization(
                authorization_path,
                context,
            )
            self.assertTrue(permitted["provider_launch_authorized"])
            with self.assertRaisesRegex(
                Phase2GateError,
                "model",
            ):
                require_live_provider_authorization(
                    authorization_path,
                    {**context, "model": "drifted-model"},
                )

    def test_live_action_denies_before_run_allocation_without_authorization(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            (repository / "tracked.txt").write_text(
                "baseline\n",
                encoding="utf-8",
            )
            _git(repository, "add", "tracked.txt")
            _git(repository, "commit", "--quiet", "-m", "baseline")
            head = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            with patch(
                "agentteam_runtime.experiment_gates."
                "allocate_experiment_run"
            ) as allocate:
                with self.assertRaisesRegex(
                    Phase2GateError,
                    "authorization",
                ):
                    execute_live_calibration_action(
                        self._action_input("P2-09"),
                        {
                            "repository_root": str(repository),
                            "integration_head": head,
                            "authorization_path": str(
                                root / "missing-authorization.json"
                            ),
                        },
                        artifact_path=root / "calibration.json",
                    )
            allocate.assert_not_called()

    def test_live_action_runs_three_modes_and_one_repeat_after_authorization(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            schema_dir = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
            )
            schema_dir.mkdir(parents=True)
            shutil.copy2(
                self.SCHEMAS / "phase2_live_authorization.schema.json",
                schema_dir / "phase2_live_authorization.schema.json",
            )
            (repository / "tracked.txt").write_text(
                "baseline\n",
                encoding="utf-8",
            )
            _git(repository, "add", ".")
            _git(repository, "commit", "--quiet", "-m", "baseline")
            head = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            tree = _git(
                repository,
                "rev-parse",
                "HEAD^{tree}",
            ).stdout.strip()
            protocol = _protocol()
            protocol["repository"] = {
                "source": str(repository),
                "commit": head,
                "tree": tree,
                "git_object_format": "sha1",
            }
            protocol_path = root / "protocol.json"
            protocol_path.write_bytes(canonical_json_bytes(protocol))
            protocol_sha256 = canonical_json_sha256(protocol)
            evaluator = root / "evaluator.json"
            evaluator.write_text(
                '{"evaluator":"fixture"}\n',
                encoding="utf-8",
            )
            readiness_sha256 = "3" * 64
            context = {
                "repository_root": str(repository),
                "integration_head": head,
                "authorization_epoch_number": 2,
                "authorization_epoch_sha256": "1" * 64,
                "epoch_number": 2,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": protocol_sha256,
                "readiness_promotion_sha256": readiness_sha256,
                "model": protocol["environment"]["model"],
                "reasoning_profile": protocol["environment"][
                    "reasoning_profile"
                ],
                "max_total_tokens": protocol["budgets"][
                    "max_total_tokens"
                ],
                "max_wall_time_seconds": protocol["budgets"][
                    "max_wall_time_seconds"
                ],
                "runtime_release": _filesystem_release(
                    root / "candidate-runtime",
                    head,
                ),
                "projection_root": str(root / "projection"),
            }
            authorization = {
                "schema_version": "phase2_live_authorization.v1",
                "decision": "approved",
                "operator_identity": "phase2-test",
                "authorized_at": "2026-07-29T00:00:00Z",
                "gate_id": "P2-09",
                "epoch_number": 2,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": protocol_sha256,
                "readiness_promotion_sha256": readiness_sha256,
                "model": context["model"],
                "reasoning_profile": context["reasoning_profile"],
                "max_total_tokens": context["max_total_tokens"],
                "max_wall_time_seconds": context[
                    "max_wall_time_seconds"
                ],
                "max_inflight_model_invocations": 1,
                "modes": list(protocol["modes"]),
            }
            authorization_path = root / "authorization.json"
            publish_live_authorization(
                authorization_path,
                authorization,
                context,
            )
            context["authorization_path"] = str(authorization_path)
            allocated = {}

            def allocate(
                projection_root,
                _protocol_value,
                *,
                mode,
                repetition_index,
                stable_request_key,
                runtime_release,
            ):
                run_dir = (
                    Path(projection_root)
                    / "runs"
                    / f"{repetition_index}-{mode}"
                )
                run_dir.mkdir(parents=True)
                allocated[str(run_dir.resolve())] = mode
                return {
                    "run_dir": str(run_dir),
                    "run_manifest": {
                        "mode": mode,
                        "repetition_index": repetition_index,
                        "stable_request_key": stable_request_key,
                    },
                }

            def load_result(run_dir):
                mode = allocated[str(Path(run_dir).resolve())]
                return {
                    "bundle_sha256": hashlib.sha256(
                        str(run_dir).encode("utf-8")
                    ).hexdigest(),
                    "bundle": {
                        "mode": mode,
                        "terminal_status": "completed",
                        "acceptance_result": {"status": "passed"},
                        "started_at": "2026-07-29T00:00:00Z",
                        "finished_at": "2026-07-29T00:00:01Z",
                        "usage_coverage": {
                            "total_invocations": 1,
                            "covered_invocations": 1,
                        },
                        "usage_totals": {"total_tokens": 100},
                    },
                }

            action_input = {
                "schema_version": "phase2_gate_action_input.v1",
                "action": "run_live_calibration",
                "configuration": {
                    "authority_artifacts": {
                        "evaluator": {
                            "path": str(evaluator),
                            "sha256": hashlib.sha256(
                                evaluator.read_bytes()
                            ).hexdigest(),
                        },
                    },
                    "direct_taskpack": {
                        "path": str(root),
                        "digest_sha256": "5" * 64,
                    },
                    "repeat_mode": "single_codex",
                    "sandbox_configuration": {},
                },
            }
            context.update(
                {
                    "protocol_path": str(protocol_path),
                    "authority_roots": [
                        str(root),
                        str(repository),
                    ],
                }
            )
            drift_context = {
                **context,
                "model": "authorized-drift-model",
            }
            drift_authorization = {
                **authorization,
                "model": "authorized-drift-model",
            }
            drift_path = root / "drift-authorization.json"
            publish_live_authorization(
                drift_path,
                drift_authorization,
                drift_context,
            )
            drift_context["authorization_path"] = str(drift_path)
            with patch(
                "agentteam_runtime.experiment_gates."
                "allocate_experiment_run"
            ) as drift_allocate, patch(
                "agentteam_runtime.taskpack."
                "verify_frozen_taskpack_digest",
                return_value={"digest_sha256": "5" * 64},
            ):
                with self.assertRaisesRegex(
                    Phase2GateError,
                    "provider policy or budgets",
                ):
                    execute_live_calibration_action(
                        action_input,
                        drift_context,
                        artifact_path=root / "drift-calibration.json",
                    )
            drift_allocate.assert_not_called()
            projection_target = root / "projection-target"
            projection_target.mkdir()
            projection_link = root / "projection-link"
            projection_link.symlink_to(
                projection_target,
                target_is_directory=True,
            )
            with patch(
                "agentteam_runtime.experiment_gates."
                "allocate_experiment_run"
            ) as unsafe_allocate, patch(
                "agentteam_runtime.taskpack."
                "verify_frozen_taskpack_digest",
                return_value={"digest_sha256": "5" * 64},
            ):
                with self.assertRaisesRegex(
                    Phase2GateError,
                    "projection root is unsafe",
                ):
                    execute_live_calibration_action(
                        action_input,
                        {
                            **context,
                            "projection_root": str(projection_link),
                        },
                        artifact_path=(
                            root / "unsafe-projection-calibration.json"
                        ),
                    )
            unsafe_allocate.assert_not_called()
            with patch(
                "agentteam_runtime.experiment_gates."
                "allocate_experiment_run",
                side_effect=allocate,
            ) as allocate_mock, patch(
                "agentteam_runtime.experiment_gates."
                "execute_bound_experiment_mode",
                return_value={"sealed_result": {}},
            ) as execute_mock, patch(
                "agentteam_runtime.experiment_gates."
                "load_experiment_result_bundle",
                side_effect=load_result,
            ), patch(
                "agentteam_runtime.taskpack."
                "verify_frozen_taskpack_digest",
                return_value={"digest_sha256": "5" * 64},
            ):
                action = execute_live_calibration_action(
                    action_input,
                    context,
                    artifact_path=root / "live-calibration.json",
                )
            self.assertEqual(action["action_status"], "completed")
            self.assertEqual(action["provider_calls"], 4)
            self.assertEqual(allocate_mock.call_count, 4)
            self.assertEqual(execute_mock.call_count, 4)
            self.assertEqual(
                [
                    item["mode"]
                    for item in json.loads(
                        (root / "live-calibration.json").read_text(
                            encoding="utf-8"
                        )
                    )["mode_results"]
                ],
                list(protocol["modes"]),
            )

    def test_readiness_relation_recomputes_pilot_and_exact_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository_clone(root)
            schema_dir = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
            )
            schema_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                self.SCHEMAS / "phase2_readiness_promotion.schema.json",
                schema_dir / "phase2_readiness_promotion.schema.json",
            )
            readiness_path = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
                / "data"
                / "p0_experiment_readiness.v1.json"
            )
            readiness_path.parent.mkdir(parents=True, exist_ok=True)
            packaged_readiness = (
                Path(__file__).resolve().parents[1]
                / "agentteam_runtime"
                / "data"
                / "p0_experiment_readiness.v1.json"
            )
            readiness = json.loads(
                packaged_readiness.read_text(encoding="utf-8")
            )
            evidence = {}
            for capability in readiness["capabilities"]:
                evidence_path = (
                    repository
                    / "evidence"
                    / f"{capability['capability_id']}.json"
                )
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text(
                    '{"status":"passed"}\n',
                    encoding="utf-8",
                )
                evidence[capability["capability_id"]] = [
                    {
                        "artifact_path": evidence_path.relative_to(
                            repository
                        ).as_posix(),
                        "sha256": hashlib.sha256(
                            evidence_path.read_bytes()
                        ).hexdigest(),
                        "test_id": test_id,
                        "status": "passed",
                    }
                    for test_id in self.CAPABILITY_TEST_IDS[
                        capability["capability_id"]
                    ]
                ]
            readiness_path.write_text(
                json.dumps(readiness, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _git(repository, "add", ".")
            _git(repository, "commit", "--quiet", "-m", "readiness base")
            parent = _git(repository, "rev-parse", "HEAD").stdout.strip()
            before_sha256 = hashlib.sha256(
                readiness_path.read_bytes()
            ).hexdigest()
            for capability in readiness["capabilities"]:
                capability["status"] = "passed"
                capability["reason"] = "Phase 2 deterministic evidence."
                capability["evidence"] = sorted({
                    item["artifact_path"]
                    for item in evidence[capability["capability_id"]]
                })
            readiness["overall_status"] = "passed"
            readiness["pilot_authorized"] = True
            readiness["next_required_phase"] = "P2 live calibration"
            readiness_path.write_text(
                json.dumps(readiness, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            after_sha256 = hashlib.sha256(
                readiness_path.read_bytes()
            ).hexdigest()
            readiness_summary = build_p0_readiness_summary(
                readiness_path
            )
            _git(repository, "add", readiness_path)
            _git(repository, "commit", "--quiet", "-m", "promote readiness")
            head = _git(repository, "rev-parse", "HEAD").stdout.strip()

            protocol = root / "protocol.json"
            run_manifest = root / "run-manifest.json"
            calibration = root / "deterministic-calibration.json"
            pilot_manifest = root / "pilot-manifest.json"
            pilot_guard = root / "pilot-guard.json"
            protocol_value = _protocol()
            protocol_value["repository"] = {
                "source": str(repository),
                "commit": head,
                "tree": _git(
                    repository,
                    "rev-parse",
                    "HEAD^{tree}",
                ).stdout.strip(),
                "git_object_format": "sha1",
            }
            protocol.write_bytes(
                canonical_json_bytes(protocol_value)
            )
            run_manifest.write_bytes(
                canonical_json_bytes(
                    build_experiment_run_manifest(
                        protocol_value,
                        mode="agentteam_full",
                        repetition_index=0,
                        stable_request_key="phase2-pilot",
                    )
                )
            )
            self._write_calibration_report(calibration, parent)
            pilot_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": (
                            "agentteam_experiment_manifest.v1"
                        ),
                        "experiment_id": "p2-calibration",
                        "instance_id": "fixture-001",
                        "repository": {
                            "source": "/tmp/repository.git",
                            "commit": head,
                            "git_object_format": "sha1",
                        },
                        "goal": {
                            "summary": "Validate Phase 2 readiness.",
                            "constraints": [
                                "Do not read evaluator-only state."
                            ],
                        },
                        "acceptance": {
                            "command": [
                                "python3",
                                "-m",
                                "unittest",
                            ]
                        },
                        "mode": "agentteam_full",
                        "runtime": {
                            "backend": "codex",
                            "model": "gpt-5.6",
                            "sandbox_policy": "workspace-write",
                        },
                        "seed": 7,
                        "blind_gold": {
                            "policy": "unavailable_to_runtime"
                        },
                        "budgets": {
                            "max_total_tokens": 1000,
                            "max_wall_time_seconds": 60,
                            "stop_boundary": "scheduler_safe",
                        },
                        "usage_contract_version": (
                            "model_invocation_usage.v1"
                        ),
                        "readiness_binding": {
                            "schema_version": (
                                "p0_experiment_readiness.v1"
                            ),
                            "record_sha256": readiness_summary[
                                "record_sha256"
                            ],
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            pilot_guard.write_text(
                json.dumps(
                    {
                        "pilot_authorized": True,
                        "provider_calls": 0,
                        "target_mutations": 0,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            candidate_release = _filesystem_release(
                root / "candidate-runtime",
                head,
            )
            artifact = {
                "schema_version": "phase2_readiness_promotion.v1",
                "controller_validation_status": "passed",
                "validated_code_sha": parent,
                "protocol_sha256": canonical_json_sha256(
                    json.loads(protocol.read_text(encoding="utf-8"))
                ),
                "run_manifest_sha256": canonical_json_sha256(
                    json.loads(
                        run_manifest.read_text(encoding="utf-8")
                    )
                ),
                "deterministic_calibration_sha256": hashlib.sha256(
                    calibration.read_bytes()
                ).hexdigest(),
                "readiness_before_sha256": before_sha256,
                "readiness_after_sha256": after_sha256,
                "capability_evidence": evidence,
                "pilot_guard_status": "passed",
                "pilot_manifest_sha256": canonical_json_sha256(
                    json.loads(
                        pilot_manifest.read_text(encoding="utf-8")
                    )
                ),
                "pilot_guard_sha256": hashlib.sha256(
                    pilot_guard.read_bytes()
                ).hexdigest(),
                "pilot_guard_provider_calls": 0,
                "pilot_guard_target_mutations": 0,
                "candidate_release": candidate_release,
                "changed_paths": [
                    "experiments/native_agentteam_runtime/m0_runtime/"
                    "agentteam_runtime/data/"
                    "p0_experiment_readiness.v1.json"
                ],
            }
            artifact_path = root / "readiness-promotion.json"
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            spec = resolve_gate_spec(
                {
                    "gate_id": "P2-08",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_readiness_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_readiness_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_readiness_promotion.schema.json"
                    ),
                    "operator_authorization_required": False,
                    "controller_action_input": self._action_input(
                        "P2-08"
                    ),
                }
            )
            relation = validate_gate_relation(
                spec,
                artifact_path,
                {
                    "repository_root": str(repository),
                    "integration_head": head,
                    "protocol_path": str(protocol),
                    "run_manifest_path": str(run_manifest),
                    "deterministic_calibration_path": str(calibration),
                    "pilot_manifest_path": str(pilot_manifest),
                    "pilot_guard_path": str(pilot_guard),
                    "runtime_release": candidate_release,
                },
            )
            self.assertEqual(relation["relation_status"], "passed")
            self.assertEqual(relation["capability_count"], 7)
            calibration.write_text(
                '{"calibration_status":"passed"}\n',
                encoding="utf-8",
            )
            artifact["deterministic_calibration_sha256"] = hashlib.sha256(
                calibration.read_bytes()
            ).hexdigest()
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Phase2GateError,
                "calibration authority is invalid",
            ):
                validate_gate_relation(
                    spec,
                    artifact_path,
                    {
                        "repository_root": str(repository),
                        "integration_head": head,
                        "protocol_path": str(protocol),
                        "run_manifest_path": str(run_manifest),
                        "deterministic_calibration_path": str(calibration),
                        "pilot_manifest_path": str(pilot_manifest),
                        "pilot_guard_path": str(pilot_guard),
                        "runtime_release": candidate_release,
                    },
                )
            self._write_calibration_report(calibration, parent)
            artifact["deterministic_calibration_sha256"] = hashlib.sha256(
                calibration.read_bytes()
            ).hexdigest()
            capability_id = "machine_readable_result_bundle"
            artifact["capability_evidence"][capability_id][0][
                "test_id"
            ] = "tests.test_experiment_harness.Phase2GateTests.test_fake"
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Phase2GateError,
                "fixed capability test",
            ):
                validate_gate_relation(
                    spec,
                    artifact_path,
                    {
                        "repository_root": str(repository),
                        "integration_head": head,
                        "protocol_path": str(protocol),
                        "run_manifest_path": str(run_manifest),
                        "deterministic_calibration_path": str(calibration),
                        "pilot_manifest_path": str(pilot_manifest),
                        "pilot_guard_path": str(pilot_guard),
                        "runtime_release": candidate_release,
                    },
                )

    def test_readiness_action_creates_exact_promoted_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository_clone(root)
            shutil.copy2(
                self.SCHEMAS / "phase2_readiness_promotion.schema.json",
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
                / "phase2_readiness_promotion.schema.json",
            )
            _git(
                repository,
                "add",
                "experiments/native_agentteam_runtime/schemas/"
                "phase2_readiness_promotion.schema.json",
            )
            if _git(
                repository,
                "status",
                "--porcelain=v1",
            ).stdout.strip():
                _git(
                    repository,
                    "commit",
                    "--quiet",
                    "-m",
                    "update readiness evidence schema",
                )
            readiness_path = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
                / "data"
                / "p0_experiment_readiness.v1.json"
            )
            readiness = json.loads(
                readiness_path.read_text(encoding="utf-8")
            )
            harness_path = (
                "experiments/native_agentteam_runtime/m0_runtime/"
                "tests/test_experiment_harness.py"
            )
            usage_path = (
                "experiments/native_agentteam_runtime/m0_runtime/"
                "tests/test_phase1_usage_end_to_end.py"
            )
            evidence = {}
            for capability_id, test_ids in (
                self.CAPABILITY_TEST_IDS.items()
            ):
                evidence_path = (
                    usage_path
                    if capability_id == "invocation_level_real_usage"
                    else harness_path
                )
                evidence_bytes = (
                    repository / evidence_path
                ).read_bytes()
                evidence[capability_id] = [
                    {
                        "artifact_path": evidence_path,
                        "sha256": hashlib.sha256(
                            evidence_bytes
                        ).hexdigest(),
                        "test_id": test_id,
                        "status": "passed",
                    }
                    for test_id in test_ids
                ]
            promoted_at = "2026-07-29T00:00:00Z"
            expected_readiness = copy.deepcopy(readiness)
            for capability in expected_readiness["capabilities"]:
                capability_id = capability["capability_id"]
                capability.update(
                    {
                        "status": "passed",
                        "reason": "Validated by the fixed Phase 2 test.",
                        "evidence": sorted(
                            {
                                item["artifact_path"]
                                for item in evidence[capability_id]
                            }
                        ),
                    }
                )
            expected_readiness.update(
                {
                    "updated_at": promoted_at,
                    "overall_status": "passed",
                    "pilot_authorized": True,
                    "next_required_phase": (
                        "P2 bounded live calibration"
                    ),
                }
            )
            protocol = root / "protocol-template.json"
            calibration = root / "calibration.json"
            parent = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            self._write_calibration_report(calibration, parent)
            protocol_template = _protocol()
            protocol_template["repository"] = {
                "source": str(repository),
                "commit": parent,
                "tree": _git(
                    repository,
                    "rev-parse",
                    "HEAD^{tree}",
                ).stdout.strip(),
                "git_object_format": "sha1",
            }
            protocol.write_bytes(
                canonical_json_bytes(protocol_template)
            )

            def binding(path):
                return {
                    "path": str(path),
                    "sha256": hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest(),
                }

            artifact_path = root / "readiness-promotion.json"
            pilot_guard_path = root / "pilot-guard.json"

            def candidate_guard(head, manifest_path):
                self.assertEqual(
                    _git(
                        repository,
                        "rev-parse",
                        "HEAD",
                    ).stdout.strip(),
                    head,
                )
                generated_manifest = json.loads(
                    Path(manifest_path).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    generated_manifest["repository"]["commit"],
                    head,
                )
                runtime_release = _filesystem_release(
                    root / "candidate-runtime",
                    head,
                )
                return {
                    "pilot_guard": {
                        "pilot_authorized": True,
                        "provider_calls": 0,
                        "target_mutations": 0,
                    },
                    "runtime_release": runtime_release,
                }

            action = execute_readiness_promotion_action(
                {
                    "schema_version": "phase2_gate_action_input.v1",
                    "action": "promote_readiness",
                    "configuration": {
                        "promoted_at": promoted_at,
                        "readiness_reasons": {
                            capability_id: (
                                "Validated by the fixed Phase 2 test."
                            )
                            for capability_id in evidence
                        },
                        "capability_evidence": evidence,
                        "authority_artifacts": {
                            "protocol_template": binding(protocol),
                            "deterministic_calibration": binding(
                                calibration
                            ),
                        },
                        "pilot_mode": "agentteam_full",
                        "pilot_repetition_index": 0,
                        "pilot_stable_request_key": "phase2-pilot",
                    },
                },
                {
                    "repository_root": str(repository),
                    "integration_worktree": str(repository),
                    "integration_head": parent,
                    "authority_roots": [str(root)],
                },
                artifact_path=artifact_path,
                pilot_guard_path=pilot_guard_path,
                protocol_path=root / "generated-protocol.json",
                run_manifest_path=root / "generated-run.json",
                pilot_manifest_path=root / "generated-pilot.json",
                candidate_guard_runner=candidate_guard,
            )
            self.assertEqual(action["action_status"], "completed")
            self.assertEqual(
                _git(
                    repository,
                    "diff",
                    "--name-only",
                    f"{parent}..{action['integration_head']}",
                ).stdout.splitlines(),
                [
                    "experiments/native_agentteam_runtime/m0_runtime/"
                    "agentteam_runtime/data/"
                    "p0_experiment_readiness.v1.json"
                ],
            )
            spec = resolve_gate_spec(
                {
                    "gate_id": "P2-08",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_readiness_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_readiness_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_readiness_promotion.schema.json"
                    ),
                    "operator_authorization_required": False,
                    "controller_action_input": self._action_input(
                        "P2-08"
                    ),
                }
            )
            relation = validate_gate_relation(
                spec,
                artifact_path,
                {
                    "repository_root": str(repository),
                    "integration_head": action["integration_head"],
                    **action["relation_context"],
                },
            )
            self.assertEqual(relation["relation_status"], "passed")

    def test_live_calibration_relation_recomputes_sealed_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ExperimentCalibrationTests()._real_calibration_fixture(
                root
            )
            repository = Path(
                fixture["protocol"]["repository"]["source"]
            )
            schema_dir = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
            )
            schema_dir.mkdir(parents=True)
            for name in (
                "phase2_calibration.schema.json",
                "phase2_live_authorization.schema.json",
            ):
                shutil.copy2(self.SCHEMAS / name, schema_dir / name)
            protocol_sha256 = canonical_json_sha256(fixture["protocol"])
            readiness_sha256 = "3" * 64
            integration_head = fixture["protocol"]["repository"]["commit"]
            budgets = fixture["protocol"]["budgets"]
            context = {
                "repository_root": str(repository),
                "integration_head": integration_head,
                "epoch_number": 4,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": protocol_sha256,
                "readiness_promotion_sha256": readiness_sha256,
                "model": fixture["protocol"]["environment"]["model"],
                "reasoning_profile": fixture["protocol"][
                    "environment"
                ]["reasoning_profile"],
                "max_total_tokens": budgets["max_total_tokens"],
                "max_wall_time_seconds": budgets[
                    "max_wall_time_seconds"
                ],
            }
            authorization = {
                "schema_version": "phase2_live_authorization.v1",
                "decision": "approved",
                "operator_identity": "phase2-test",
                "authorized_at": "2026-07-26T00:00:00Z",
                "gate_id": "P2-09",
                "epoch_number": 4,
                "epoch_sha256": "1" * 64,
                "protocol_sha256": protocol_sha256,
                "readiness_promotion_sha256": readiness_sha256,
                "model": context["model"],
                "reasoning_profile": context["reasoning_profile"],
                "max_total_tokens": budgets["max_total_tokens"],
                "max_wall_time_seconds": budgets[
                    "max_wall_time_seconds"
                ],
                "max_inflight_model_invocations": 1,
                "modes": [
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                ],
            }
            authorization_path = root / "authorization.json"
            authorization_publication = publish_live_authorization(
                authorization_path,
                authorization,
                context,
            )
            primary_by_mode = {
                item["manifest"]["mode"]: item
                for item in fixture["primary"]
            }

            def result_item(run):
                loaded = load_experiment_result_bundle(run["run_dir"])
                bundle = loaded["bundle"]
                started = datetime.fromisoformat(
                    bundle["started_at"].replace("Z", "+00:00")
                )
                finished = datetime.fromisoformat(
                    bundle["finished_at"].replace("Z", "+00:00")
                )
                return {
                    "mode": bundle["mode"],
                    "result_bundle_sha256": loaded["bundle_sha256"],
                    "total_tokens": bundle["usage_totals"][
                        "total_tokens"
                    ],
                    "wall_time_seconds": (
                        finished - started
                    ).total_seconds(),
                }

            artifact = {
                "schema_version": "phase2_calibration.v1",
                "controller_validation_status": "passed",
                "validated_code_sha": integration_head,
                "readiness_promotion_sha256": readiness_sha256,
                "protocol_sha256": protocol_sha256,
                "pilot_guard_status": "passed",
                "operator_authorization_sha256": (
                    authorization_publication["sha256"]
                ),
                "runtime_release": fixture["release"],
                "mode_results": [
                    result_item(primary_by_mode[mode])
                    for mode in (
                        "single_codex",
                        "agentteam_direct",
                        "agentteam_full",
                    )
                ],
                "repeat_result": result_item(fixture["repeat"]),
                "lifecycle_coverage_percent": 100,
                "token_coverage_percent": 100,
                "projection_rebuild_status": "passed",
            }
            artifact_path = root / "live-calibration.json"
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            spec = resolve_gate_spec(
                {
                    "gate_id": "P2-09",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_live_calibration_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_live_calibration_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_calibration.schema.json"
                    ),
                    "operator_authorization_required": True,
                    "operator_authorization_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_live_authorization.schema.json"
                    ),
                    "controller_action_input": self._action_input(
                        "P2-09"
                    ),
                }
            )
            relation_context = {
                **context,
                "authorization_path": str(authorization_path),
                "mode_run_records": {
                    mode: primary_by_mode[mode]
                    for mode in (
                        "single_codex",
                        "agentteam_direct",
                        "agentteam_full",
                    )
                },
                "repeat_run_record": fixture["repeat"],
                "projection_root": str(fixture["projection_root"]),
                "protocol_path": str(
                    fixture["projection_root"]
                    / "protocols"
                    / f"{protocol_sha256}.json"
                ),
                "runtime_release": fixture["release"],
            }
            expected_provider_calls = sum(
                load_experiment_result_bundle(run["run_dir"])[
                    "bundle"
                ]["usage_coverage"]["total_invocations"]
                for run in [
                    *fixture["primary"],
                    fixture["repeat"],
                ]
            )
            launcher = Mock(
                return_value={
                    "artifact_path": str(artifact_path),
                    "provider_calls": expected_provider_calls,
                    "target_mutations": 0,
                }
            )
            controlled = run_gate_controller(
                spec,
                root / "artifact-created-by-launcher.json",
                relation_context,
                result_path=root / "live-controller-result.json",
                provider_launcher=launcher,
            )
            launcher.assert_called_once()
            self.assertEqual(
                controlled["provider_calls"],
                expected_provider_calls,
            )
            relation = validate_gate_relation(
                spec,
                artifact_path,
                relation_context,
            )
            self.assertEqual(relation["relation_status"], "passed")
            report = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "implementation_artifacts"
                / "reports"
                / "phase2-experiment-harness.md"
            )
            roadmap = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "implementation_artifacts"
                / "native_runtime_roadmap.md"
            )
            report.parent.mkdir(parents=True, exist_ok=True)
            roadmap.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# Phase 2 report\n", encoding="utf-8")
            roadmap.write_text("# Roadmap\n", encoding="utf-8")
            _git(repository, "add", report, roadmap)
            _git(
                repository,
                "commit",
                "--quiet",
                "-m",
                "finalize phase 2 report",
            )
            final_head = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            historical_context = {
                **relation_context,
                "integration_head": final_head,
                "epoch_number": 5,
                "epoch_sha256": "9" * 64,
                "authorization_epoch_number": 4,
                "authorization_epoch_sha256": "1" * 64,
            }
            historical = validate_gate_relation(
                spec,
                artifact_path,
                historical_context,
            )
            self.assertEqual(
                historical["validated_code_sha"],
                integration_head,
            )
            self.assertEqual(
                historical["integration_head"],
                final_head,
            )
            artifact["mode_results"][0]["total_tokens"] += 1
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                Phase2GateError,
                "token total",
            ):
                validate_gate_relation(
                    spec,
                    artifact_path,
                    historical_context,
                )

    def test_finalization_relation_recomputes_exact_git_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            schema_path = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
                / "phase2_finalization.schema.json"
            )
            schema_path.parent.mkdir(parents=True)
            shutil.copy2(
                self.SCHEMAS / "phase2_finalization.schema.json",
                schema_path,
            )
            roadmap = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "implementation_artifacts"
                / "native_runtime_roadmap.md"
            )
            report = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "implementation_artifacts"
                / "reports"
                / "phase2-experiment-harness.md"
            )
            roadmap.parent.mkdir(parents=True, exist_ok=True)
            report.parent.mkdir(parents=True, exist_ok=True)
            roadmap.write_text("before roadmap\n", encoding="utf-8")
            report.write_text("before report\n", encoding="utf-8")
            _git(repository, "add", ".")
            _git(repository, "commit", "--quiet", "-m", "baseline")
            parent = _git(repository, "rev-parse", "HEAD").stdout.strip()
            roadmap.write_text("after roadmap\n", encoding="utf-8")
            report.write_text("after report\n", encoding="utf-8")
            _git(repository, "add", ".")
            _git(repository, "commit", "--quiet", "-m", "final report")
            head = _git(repository, "rev-parse", "HEAD").stdout.strip()
            verification_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "pass",
            ]
            empty_sha256 = hashlib.sha256(b"").hexdigest()
            verification = root / "full-verification.json"
            verification.write_text(
                json.dumps(
                    {
                        "command": verification_command,
                        "returncode": 0,
                        "stdout_sha256": empty_sha256,
                        "stderr_sha256": empty_sha256,
                        "normalized_stdout_sha256": empty_sha256,
                        "normalized_stderr_sha256": empty_sha256,
                        "integration_head": head,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            artifact = {
                "schema_version": "phase2_finalization.v1",
                "controller_validation_status": "passed",
                "validated_code_sha": parent,
                "final_report_sha": head,
                "readiness_promotion_sha256": "4" * 64,
                "live_calibration_sha256": "5" * 64,
                "report_sha256": hashlib.sha256(
                    report.read_bytes()
                ).hexdigest(),
                "roadmap_sha256": hashlib.sha256(
                    roadmap.read_bytes()
                ).hexdigest(),
                "full_verification_sha256": hashlib.sha256(
                    verification.read_bytes()
                ).hexdigest(),
                "changed_paths": [
                    "experiments/native_agentteam_runtime/"
                    "implementation_artifacts/native_runtime_roadmap.md",
                    "experiments/native_agentteam_runtime/"
                    "implementation_artifacts/reports/"
                    "phase2-experiment-harness.md",
                ],
                "merge_recommendation": "ready_for_operator_review",
            }
            artifact_path = root / "finalization.json"
            artifact_path.write_text(
                json.dumps(artifact, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            spec = resolve_gate_spec(
                {
                    "gate_id": "P2-10",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_finalization_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_finalization_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_finalization.schema.json"
                    ),
                    "operator_authorization_required": False,
                    "controller_action_input": self._action_input(
                        "P2-10"
                    ),
                }
            )
            context = {
                "repository_root": str(repository),
                "integration_head": head,
                "readiness_promotion_sha256": "4" * 64,
                "live_calibration_sha256": "5" * 64,
                "full_verification_path": str(verification),
                "full_verification_command": verification_command,
                "integration_worktree": str(repository),
                "prior_gate_validated_code": {"P2-09": parent},
                "prior_gate_evidence": {
                    "P2-08": "4" * 64,
                    "P2-09": "5" * 64,
                },
            }
            relation = validate_gate_relation(
                spec,
                artifact_path,
                context,
            )
            self.assertEqual(relation["relation_status"], "passed")
            (repository / "unexpected.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            _git(repository, "add", "unexpected.txt")
            _git(repository, "commit", "--quiet", "-m", "unexpected")
            unexpected_head = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            with self.assertRaisesRegex(
                Phase2GateError,
                "single-child|exact report paths",
            ):
                validate_gate_relation(
                    spec,
                    artifact_path,
                    {**context, "integration_head": unexpected_head},
                )

    def test_finalization_action_creates_exact_report_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = self._repository(root)
            schema_path = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "schemas"
                / "phase2_finalization.schema.json"
            )
            schema_path.parent.mkdir(parents=True)
            shutil.copy2(
                self.SCHEMAS / "phase2_finalization.schema.json",
                schema_path,
            )
            roadmap = (
                repository
                / "experiments"
                / "native_agentteam_runtime"
                / "implementation_artifacts"
                / "native_runtime_roadmap.md"
            )
            roadmap.parent.mkdir(parents=True)
            roadmap.write_text("# Roadmap\n", encoding="utf-8")
            _git(repository, "add", ".")
            _git(repository, "commit", "--quiet", "-m", "baseline")
            parent = _git(
                repository,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            readiness = root / "readiness.json"
            calibration = root / "calibration.json"
            readiness.write_text(
                json.dumps(
                    {
                        "controller_validation_status": "passed",
                        "validated_code_sha": parent,
                        "capability_evidence": {
                            str(index): []
                            for index in range(7)
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            calibration.write_text(
                json.dumps(
                    {
                        "mode_results": [
                            {
                                "mode": mode,
                                "total_tokens": index + 1,
                                "wall_time_seconds": index + 0.25,
                            }
                            for index, mode in enumerate(
                                (
                                    "single_codex",
                                    "agentteam_direct",
                                    "agentteam_full",
                                )
                            )
                        ],
                        "repeat_result": {
                            "mode": "single_codex",
                        },
                        "lifecycle_coverage_percent": 100,
                        "token_coverage_percent": 100,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            prior = {
                "readiness_promotion": {
                    "path": str(readiness),
                    "sha256": hashlib.sha256(
                        readiness.read_bytes()
                    ).hexdigest(),
                },
                "live_calibration": {
                    "path": str(calibration),
                    "sha256": hashlib.sha256(
                        calibration.read_bytes()
                    ).hexdigest(),
                },
            }
            artifact_path = root / "finalization.json"
            verification_path = root / "verification.json"
            verification_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "pass",
            ]
            action = execute_phase2_finalization_action(
                {
                    "schema_version": "phase2_gate_action_input.v1",
                    "action": "finalize_phase2",
                    "configuration": {
                        "finalized_at": "2026-07-29T00:00:00Z",
                    },
                },
                {
                    "repository_root": str(repository),
                    "integration_worktree": str(repository),
                    "integration_head": parent,
                    "prior_artifacts": prior,
                    "full_verification_command": verification_command,
                    "authority_roots": [str(root)],
                },
                artifact_path=artifact_path,
                verification_path=verification_path,
            )
            self.assertEqual(action["action_status"], "completed")
            final_head = action["integration_head"]
            self.assertEqual(
                _git(
                    repository,
                    "diff",
                    "--name-only",
                    f"{parent}..{final_head}",
                ).stdout.splitlines(),
                [
                    "experiments/native_agentteam_runtime/"
                    "implementation_artifacts/native_runtime_roadmap.md",
                    "experiments/native_agentteam_runtime/"
                    "implementation_artifacts/reports/"
                    "phase2-experiment-harness.md",
                ],
            )
            spec = resolve_gate_spec(
                {
                    "gate_id": "P2-10",
                    "executor": "deterministic_controller",
                    "controller_entrypoint": (
                        "phase2_finalization_controller_v1"
                    ),
                    "relation_validator": (
                        "phase2_finalization_relation_v1"
                    ),
                    "evidence_schema": (
                        "experiments/native_agentteam_runtime/schemas/"
                        "phase2_finalization.schema.json"
                    ),
                    "operator_authorization_required": False,
                    "controller_action_input": self._action_input(
                        "P2-10"
                    ),
                }
            )
            relation = validate_gate_relation(
                spec,
                artifact_path,
                {
                    "repository_root": str(repository),
                    "integration_head": final_head,
                    "integration_worktree": str(repository),
                    "readiness_promotion_sha256": prior[
                        "readiness_promotion"
                    ]["sha256"],
                    "live_calibration_sha256": prior[
                        "live_calibration"
                    ]["sha256"],
                    "full_verification_path": str(
                        verification_path
                    ),
                    "full_verification_command": verification_command,
                    "prior_gate_validated_code": {
                        "P2-09": parent,
                    },
                    "prior_gate_evidence": {
                        "P2-08": prior["readiness_promotion"][
                            "sha256"
                        ],
                        "P2-09": prior["live_calibration"]["sha256"],
                    },
                },
            )
            self.assertEqual(relation["relation_status"], "passed")


if __name__ == "__main__":
    unittest.main()

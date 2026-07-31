"""Equal-input execution-mode orchestration for Phase 2 experiments."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .experiment_contract import (
    acquire_controller_lease,
    canonical_json_sha256,
    publish_immutable_json,
    validate_resume_binding,
    validate_experiment_protocol,
    validate_experiment_run_manifest,
)
from .experiment_controller import (
    create_experiment_controller,
    load_experiment_controller,
    validate_experiment_controller_reference,
)
from .experiment_results import (
    build_experiment_result_bundle,
    load_experiment_result_bundle,
    measure_experiment_artifacts,
    seal_experiment_result_bundle,
)
from .experiment_workspace import (
    CLEANUP_RECEIPT_FILE_NAME,
    allocate_clean_snapshot,
    cleanup_clean_snapshot,
    load_clean_snapshot_cleanup_receipt,
    publish_clean_snapshot_cleanup_receipt,
    verify_clean_snapshot,
)
from .experiment_sandbox import (
    build_provider_sandbox_descriptor,
    certify_candidate_repository,
    ExperimentEvaluationBlocked,
    experiment_lifecycle_authority_root,
    publish_evaluator_reference,
    publish_experiment_launch_registration,
    publish_experiment_mode_authority,
    publish_experiment_protocol_reference,
    publish_registered_model_invocation_set_reference,
    publish_provider_sandbox_reference,
    publish_scan_scope_reference,
    run_trusted_argv_evaluator,
    load_model_invocation_set_reference,
)
from .m0_runtime import create_independent_attempt_workspace
from .model_invocation import (
    ModelInvocationCall,
    is_supported_codex_command,
)
from .taskpack import (
    TaskpackValidationError,
    verify_frozen_taskpack_digest,
)


MODE_RESULT_SCHEMA_VERSION = "experiment_mode_lifecycle_result.v1"
_MODES = {
    "single_codex",
    "agentteam_direct",
    "agentteam_full",
}
_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "infrastructure_failed",
    "interrupted",
    "budget_stopped",
}


class ExperimentModeError(RuntimeError):
    """Raised when a mode violates the common experiment contract."""


@dataclass(frozen=True)
class ExperimentModeRequest:
    protocol: dict
    run_manifest: dict
    run_dir: str
    project_root: str
    authority_root: str
    controller_reference: dict
    common_contract_sha256: str
    model_policy: dict
    sandbox_configuration: dict | None
    runtime_release_identity: dict


class ExperimentCommonFinalizer:
    """Run the protocol evaluator and seal one authoritative P2-05 result."""

    def __init__(
        self,
        *,
        evaluator_artifact,
        evaluator_bytes=None,
        runtime_release_identity,
    ):
        evaluator_artifact = Path(evaluator_artifact).resolve(
            strict=True
        )
        if (
            evaluator_artifact.is_symlink()
            or not evaluator_artifact.is_file()
        ):
            raise ExperimentModeError(
                "trusted experiment evaluator is unavailable"
            )
        if evaluator_bytes is None:
            evaluator_bytes = evaluator_artifact.read_bytes()
        if not isinstance(evaluator_bytes, bytes):
            raise ExperimentModeError(
                "trusted experiment evaluator bytes are invalid"
            )
        evaluator_sha256 = hashlib.sha256(evaluator_bytes).hexdigest()
        if not isinstance(runtime_release_identity, dict):
            raise ExperimentModeError(
                "runtime release identity is unavailable"
            )
        self.evaluator_artifact = evaluator_artifact
        self.evaluator_bytes = evaluator_bytes
        self.evaluator_sha256 = evaluator_sha256
        self.runtime_release_identity = copy.deepcopy(
            runtime_release_identity
        )

    def __call__(
        self,
        *,
        request,
        mode_result,
        invocation_set_reference,
    ):
        candidate_workspace, candidate_state = (
            _validated_candidate_workspace(
                request,
                mode_result,
            )
        )
        if (
            self.evaluator_sha256
            != request.protocol["evaluator"]["artifact_sha256"]
        ):
            raise ExperimentModeError(
                "trusted evaluator digest differs from protocol"
            )
        retained_roots = _common_retained_roots(
            request,
            mode_result,
        )
        protocol_reference = publish_experiment_protocol_reference(
            request.authority_root,
            request.protocol,
        )
        evaluator_reference = publish_evaluator_reference(
            request.authority_root,
            self.evaluator_artifact,
            source_bytes=self.evaluator_bytes,
        )
        candidate_sandbox = build_provider_sandbox_descriptor(
            candidate_workspace,
            runtime_views=request.sandbox_configuration[
                "runtime_views"
            ],
            library_views=request.sandbox_configuration[
                "library_views"
            ],
            credential_mounts=request.sandbox_configuration[
                "credential_mounts"
            ],
            environment=request.sandbox_configuration[
                "environment"
            ],
            repository_identity={
                "commit": candidate_state["head_commit"],
                "tree": candidate_state["head_tree"],
                "git_object_format": candidate_state[
                    "git_object_format"
                ],
            },
            forbidden_paths=[
                request.sandbox_configuration["canary_path"]
            ],
        )
        candidate_sandbox_reference = (
            publish_provider_sandbox_reference(
                request.authority_root,
                candidate_sandbox,
                request.sandbox_configuration["canary_path"],
                reference_id="candidate-evaluation",
            )
        )
        scan_scope_reference = publish_scan_scope_reference(
            request.authority_root,
            retained_roots,
        )
        evaluation_path = (
            Path(request.run_dir)
            / "artifacts"
            / "common-evaluation.json"
        )
        try:
            evaluation = run_trusted_argv_evaluator(
                authority_root=request.authority_root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=(
                    candidate_sandbox_reference
                ),
                experiment_protocol_reference=protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=request.protocol["acceptance"]["command"],
                cwd=candidate_workspace,
                evaluator_reference=evaluator_reference,
                canary_path=request.sandbox_configuration[
                    "canary_path"
                ],
                timeout_seconds=request.protocol["acceptance"][
                    "timeout_seconds"
                ],
                evidence_path=evaluation_path,
            )
        except ExperimentEvaluationBlocked as exc:
            evaluation = exc.evidence
        evaluation = _validate_common_evaluation(
            request,
            evaluation,
            expected_path=evaluation_path,
        )
        bundle = _build_common_result_bundle(
            request,
            mode_result,
            invocation_set_reference,
            evaluation,
            candidate_state,
            self.runtime_release_identity,
            retained_roots,
            scan_scope_reference,
        )
        seal_experiment_result_bundle(
            request.run_dir,
            bundle,
            protocol=request.protocol,
            run_manifest=request.run_manifest,
            runtime_release_identity=self.runtime_release_identity,
            authority_root=request.authority_root,
            evaluation_path=evaluation_path,
            invocation_set_reference=invocation_set_reference,
            protocol_reference=protocol_reference,
            provider_sandbox_reference=(
                candidate_sandbox_reference
            ),
            scan_scope_reference=scan_scope_reference,
            retained_roots=retained_roots,
            canary_path=request.sandbox_configuration["canary_path"],
        )
        sealed = load_experiment_result_bundle(request.run_dir)
        _validate_sealed_mode_result(
            request,
            mode_result,
            invocation_set_reference,
            evaluation,
            sealed,
        )
        cleanup = _ensure_sealed_result_cleanup(
            request.run_dir,
            sealed,
        )
        return {
            **copy.deepcopy(mode_result),
            "evaluation": evaluation,
            "sealed_result": {
                "result_dir": sealed["result_dir"],
                "bundle_sha256": sealed["bundle_sha256"],
                "terminal_status": sealed["bundle"][
                    "terminal_status"
                ],
                "acceptance_status": sealed["bundle"][
                    "acceptance_result"
                ]["status"],
                "cleanup_status": cleanup["receipt"][
                    "cleanup_status"
                ],
            },
            "cleanup_receipt": copy.deepcopy(cleanup),
            "invocation_set_reference": copy.deepcopy(
                invocation_set_reference
            ),
        }


class ExperimentModeController:
    """Validate one run and dispatch it through exactly one mode adapter."""

    def __init__(
        self,
        *,
        protocol,
        run_manifest,
        run_dir,
        project_root,
        authority_root,
        controller_reference,
        sandbox_configuration,
        common_finalizer,
        runtime_release_identity,
    ):
        protocol = copy.deepcopy(protocol)
        run_manifest = copy.deepcopy(run_manifest)
        validate_experiment_protocol(protocol)
        validate_experiment_run_manifest(run_manifest, protocol)
        if run_manifest["mode"] not in _MODES:
            raise ExperimentModeError("unsupported experiment mode")
        if run_manifest["mode"] not in protocol["modes"]:
            raise ExperimentModeError(
                "run mode is absent from the protocol"
            )
        run_dir = Path(run_dir).resolve(strict=True)
        project_root = Path(project_root).resolve(strict=True)
        authority_root = Path(authority_root).resolve(strict=True)
        if run_dir.name != run_manifest["experiment_run_id"]:
            raise ExperimentModeError(
                "mode run directory differs from run manifest"
            )
        if project_root != (run_dir / "repository").resolve(
            strict=False
        ):
            raise ExperimentModeError(
                "mode project root is not the run-local snapshot"
            )
        try:
            verify_clean_snapshot(
                project_root,
                protocol["repository"],
            )
        except Exception as exc:
            raise ExperimentModeError(
                "mode project root is not the verified clean snapshot"
            ) from exc
        controller_reference = (
            validate_experiment_controller_reference(
                controller_reference,
                expected_protocol_id=protocol["experiment_id"],
                expected_protocol_sha256=canonical_json_sha256(
                    protocol
                ),
                expected_scored=protocol["scored"],
                expected_operator_limits=protocol[
                    "operator_limits"
                ],
                expected_budgets=protocol["budgets"],
            )
        )
        self.protocol = protocol
        self.run_manifest = run_manifest
        self.run_dir = run_dir
        self.project_root = project_root
        self.authority_root = authority_root
        self.controller_reference = controller_reference
        if not isinstance(runtime_release_identity, dict):
            raise ExperimentModeError(
                "mode runtime release identity is unavailable"
            )
        self.runtime_release_identity = copy.deepcopy(
            runtime_release_identity
        )
        _validate_sandbox_configuration(sandbox_configuration)
        self.sandbox_configuration = copy.deepcopy(
            sandbox_configuration
        )
        sandbox_binding = publish_immutable_json(
            Path(controller_reference["controller_root"])
            / "experiment-sandbox-configuration.json",
            self.sandbox_configuration,
            label="experiment sandbox configuration",
        )
        self.sandbox_configuration_sha256 = sandbox_binding[
            "sha256"
        ]
        if type(common_finalizer) is not ExperimentCommonFinalizer:
            raise ExperimentModeError(
                "common experiment finalizer authority is unavailable"
            )
        if (
            common_finalizer.runtime_release_identity
            != self.runtime_release_identity
        ):
            raise ExperimentModeError(
                "common finalizer release differs from run authority"
            )
        self.common_finalizer = common_finalizer
        self.common_contract_sha256 = canonical_json_sha256(
            _common_mode_contract(
                protocol,
                self.sandbox_configuration_sha256,
            )
        )
        publish_experiment_mode_authority(
            authority_root,
            experiment_run_id=run_manifest["experiment_run_id"],
            protocol_sha256=run_manifest["protocol_sha256"],
            run_manifest_sha256=canonical_json_sha256(run_manifest),
            mode=run_manifest["mode"],
            model_policy=_model_policy(protocol),
            controller_reference=controller_reference,
            sandbox_configuration_sha256=(
                self.sandbox_configuration_sha256
            ),
        )
        _publish_mode_order_authority(
            controller_reference["controller_root"],
            protocol,
        )

    def execute(self, adapter):
        mode = self.run_manifest["mode"]
        if getattr(adapter, "mode", None) != mode:
            raise ExperimentModeError(
                "adapter mode differs from run manifest"
            )
        expected_adapter = {
            "single_codex": SingleCodexModeAdapter,
            "agentteam_direct": AgentTeamDirectModeAdapter,
            "agentteam_full": AgentTeamFullModeAdapter,
        }[mode]
        if type(adapter) is not expected_adapter:
            raise ExperimentModeError(
                "mode requires its concrete protocol adapter"
            )
        request = ExperimentModeRequest(
            protocol=copy.deepcopy(self.protocol),
            run_manifest=copy.deepcopy(self.run_manifest),
            run_dir=str(self.run_dir),
            project_root=str(self.project_root),
            authority_root=str(self.authority_root),
            controller_reference=copy.deepcopy(
                self.controller_reference
            ),
            common_contract_sha256=self.common_contract_sha256,
            model_policy=_model_policy(self.protocol),
            sandbox_configuration=copy.deepcopy(
                self.sandbox_configuration
            ),
            runtime_release_identity=copy.deepcopy(
                self.runtime_release_identity
            ),
        )
        adapter.preflight(request)
        sequence = _begin_mode_execution(
            self.controller_reference["controller_root"],
            self.protocol,
            self.run_manifest,
        )
        if sequence["execution_status"] == "already_completed":
            return _replayed_mode_result(
                self.run_dir,
                self.run_manifest,
            )
        if sequence["execution_status"] == "previously_failed":
            raise ExperimentModeError(
                "mode execution previously failed; inspect terminal authority"
            )
        if sequence["execution_status"] == "interrupted":
            error = ExperimentModeError(
                "interrupted mode execution requires lifecycle recovery"
            )
            recovery_status = _reconcile_mode_invocations(request)
            if recovery_status == "terminal_ready":
                recovered = self._finalize_adapter_failure(
                    request,
                    mode,
                    error,
                    invocation_set_reference=None,
                    mode_result=None,
                    adapter_result=None,
                )
                if recovered is not None:
                    _complete_mode_execution(
                        self.controller_reference["controller_root"],
                        self.protocol,
                        self.run_manifest,
                        sealed_result=recovered["sealed_result"],
                    )
                    return recovered
            if recovery_status == "no_provider_started":
                _fail_mode_execution(
                    self.controller_reference["controller_root"],
                    self.protocol,
                    self.run_manifest,
                    run_dir=self.run_dir,
                    error=error,
                )
            raise error
        invocation_set_reference = None
        adapter_result = None
        result = None
        try:
            adapter_result = adapter.execute(request)
            invocation_set_reference = (
                publish_registered_model_invocation_set_reference(
                    request.authority_root,
                    request.run_manifest["experiment_run_id"],
                )
            )
            invocation_manifest = load_model_invocation_set_reference(
                invocation_set_reference,
                request.authority_root,
            )
            invocation_count = sum(
                len(item["invocation_ids"])
                for item in invocation_manifest["invocation_sets"]
            )
            result = _normalize_mode_result(
                request,
                mode,
                adapter_result,
                authoritative_invocation_count=invocation_count,
            )
            finalized = self.common_finalizer(
                request=request,
                mode_result=copy.deepcopy(result),
                invocation_set_reference=copy.deepcopy(
                    invocation_set_reference
                ),
            )
            if not isinstance(finalized, dict):
                raise ExperimentModeError(
                    "common experiment finalizer returned an invalid result"
                )
        except Exception as exc:
            if adapter_result is None:
                adapter_result = adapter.failure_context(request)
            recovered = self._finalize_adapter_failure(
                request,
                mode,
                exc,
                invocation_set_reference=invocation_set_reference,
                mode_result=result,
                adapter_result=adapter_result,
            )
            if recovered is not None:
                _complete_mode_execution(
                    self.controller_reference["controller_root"],
                    self.protocol,
                    self.run_manifest,
                    sealed_result=recovered["sealed_result"],
                )
                return recovered
            recovery_status = _reconcile_mode_invocations(request)
            if recovery_status == "terminal_ready":
                recovered = self._finalize_adapter_failure(
                    request,
                    mode,
                    exc,
                    invocation_set_reference=None,
                    mode_result=result,
                    adapter_result=adapter_result,
                )
                if recovered is not None:
                    _complete_mode_execution(
                        self.controller_reference["controller_root"],
                        self.protocol,
                        self.run_manifest,
                        sealed_result=recovered["sealed_result"],
                    )
                    return recovered
            if recovery_status != "no_provider_started":
                raise ExperimentModeError(
                    "interrupted provider remains fenced: "
                    f"{recovery_status}"
                ) from exc
            _fail_mode_execution(
                self.controller_reference["controller_root"],
                self.protocol,
                self.run_manifest,
                run_dir=self.run_dir,
                error=exc,
            )
            raise
        _complete_mode_execution(
            self.controller_reference["controller_root"],
            self.protocol,
            self.run_manifest,
            sealed_result=finalized["sealed_result"],
        )
        return finalized

    def _finalize_adapter_failure(
        self,
        request,
        mode,
        error,
        *,
        invocation_set_reference,
        mode_result,
        adapter_result,
    ):
        try:
            if invocation_set_reference is None:
                invocation_set_reference = (
                    publish_registered_model_invocation_set_reference(
                        request.authority_root,
                        request.run_manifest["experiment_run_id"],
                    )
                )
            invocation_manifest = load_model_invocation_set_reference(
                invocation_set_reference,
                request.authority_root,
            )
            invocation_count = sum(
                len(item["invocation_ids"])
                for item in invocation_manifest["invocation_sets"]
            )
            failure_payload = {
                "terminal_status": "infrastructure_failed",
                "provider_invocation_count": invocation_count,
                "candidate_workspace": request.project_root,
                "taskpack": None,
                "adapter_output": {},
            }
            if isinstance(adapter_result, dict):
                failure_payload.update(copy.deepcopy(adapter_result))
            failure_payload["terminal_status"] = (
                "infrastructure_failed"
            )
            failure_payload["provider_invocation_count"] = (
                invocation_count
            )
            if isinstance(mode_result, dict):
                failure_result = copy.deepcopy(mode_result)
                failure_result["terminal_status"] = (
                    "infrastructure_failed"
                )
                failure_result["provider_invocation_count"] = (
                    invocation_count
                )
            else:
                failure_result = _normalize_mode_result(
                    request,
                    mode,
                    failure_payload,
                    authoritative_invocation_count=invocation_count,
                )
            try:
                _validated_candidate_workspace(
                    request,
                    failure_result,
                )
            except ExperimentModeError:
                failure_result["candidate_workspace"] = (
                    request.project_root
                )
            prior_output = failure_result.get("adapter_output")
            if not isinstance(prior_output, dict):
                prior_output = {}
            failure_result["adapter_output"] = {
                **prior_output,
                "failure_recovery": {
                    "terminal_status": "infrastructure_failed",
                    "error_type": type(error).__name__,
                    "error_sha256": hashlib.sha256(
                        str(error).encode("utf-8")
                    ).hexdigest(),
                },
            }
            return self.common_finalizer(
                request=request,
                mode_result=failure_result,
                invocation_set_reference=invocation_set_reference,
            )
        except Exception:
            return None


def execute_bound_experiment_mode(
    bound_run,
    *,
    sandbox_configuration,
    adapter,
    common_finalizer,
):
    """Execute one previously allocated immutable run through its mode."""

    if (
        not isinstance(bound_run, dict)
        or not isinstance(bound_run.get("run_manifest"), dict)
        or not bound_run.get("run_dir")
        or not isinstance(bound_run.get("binding"), dict)
        or not isinstance(
            bound_run["binding"].get("runtime_release"),
            dict,
        )
    ):
        raise ExperimentModeError(
            "bound experiment run is invalid"
        )
    protocol = bound_run.get("protocol")
    if protocol is None and bound_run.get("protocol_path"):
        protocol_path = Path(bound_run["protocol_path"]).resolve(
            strict=True
        )
        if protocol_path.is_symlink() or not protocol_path.is_file():
            raise ExperimentModeError(
                "bound experiment protocol authority is unsafe"
            )
        try:
            protocol = json.loads(
                protocol_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ExperimentModeError(
                "bound experiment protocol authority is unreadable"
            ) from exc
    if not isinstance(protocol, dict):
        raise ExperimentModeError(
            "bound experiment protocol is unavailable"
        )
    protocol = copy.deepcopy(protocol)
    run_manifest = copy.deepcopy(bound_run["run_manifest"])
    validate_experiment_protocol(protocol)
    validate_experiment_run_manifest(run_manifest, protocol)
    if canonical_json_sha256(protocol) != bound_run["binding"].get(
        "protocol_sha256"
    ):
        raise ExperimentModeError(
            "bound experiment protocol digest changed"
        )
    validated = validate_resume_binding(
        bound_run["run_dir"],
        protocol=protocol,
        run_manifest=run_manifest,
        runtime_release=bound_run["binding"]["runtime_release"],
        repository=protocol["repository"],
        stable_request_key=run_manifest["stable_request_key"],
        experiment_run_id=run_manifest["experiment_run_id"],
        protocol_sha256=run_manifest["protocol_sha256"],
        run_manifest_sha256=canonical_json_sha256(run_manifest),
    )
    if validated["binding"] != bound_run["binding"]:
        raise ExperimentModeError(
            "supplied experiment binding differs from allocation authority"
        )
    protocol = validated["protocol"]
    run_manifest = validated["run_manifest"]
    run_dir = Path(validated["run_dir"]).resolve(strict=True)
    terminal_result = run_dir / "results" / "terminal"
    if terminal_result.exists() or terminal_result.is_symlink():
        return _replayed_mode_result(run_dir, run_manifest)
    snapshot = run_dir / "repository"
    if snapshot.exists():
        verify_clean_snapshot(snapshot, protocol["repository"])
    else:
        allocation = allocate_clean_snapshot(
            run_dir,
            protocol["repository"],
        )
        snapshot = Path(allocation["snapshot_path"])
    authority_root = run_dir / "authority"
    authority_root.mkdir(mode=0o700, exist_ok=True)
    controller_root = (
        run_dir.parent.parent
        / "controllers"
        / canonical_json_sha256(protocol)
    )
    controller_root.parent.mkdir(mode=0o700, exist_ok=True)
    controller = create_experiment_controller(
        controller_root,
        protocol_id=protocol["experiment_id"],
        max_total_tokens=protocol["budgets"]["max_total_tokens"],
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
    mode_controller = ExperimentModeController(
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=run_dir,
        project_root=snapshot,
        authority_root=authority_root,
        controller_reference=controller.reference,
        sandbox_configuration=sandbox_configuration,
        common_finalizer=common_finalizer,
        runtime_release_identity=bound_run["binding"][
            "runtime_release"
        ],
    )
    with acquire_controller_lease(
        run_dir,
        controller_id=f"mode-controller-{os.getpid()}",
    ):
        return mode_controller.execute(adapter)


class SingleCodexModeAdapter:
    """Execute one fresh provider call without AgentTeam task context."""

    mode = "single_codex"

    def __init__(self, provider=None):
        provider = provider or NativeSingleCodexProvider()
        if type(provider) is not NativeSingleCodexProvider:
            raise ExperimentModeError(
                "single mode requires the native Codex provider"
            )
        self.provider = provider

    def preflight(self, _request):
        return None

    def failure_context(self, request):
        return {
            "candidate_workspace": request.project_root,
            "taskpack": None,
            "adapter_output": {},
        }

    def execute(self, request):
        prompt = _single_codex_prompt(request.protocol)
        invocation_context = _experiment_invocation_context(
            request,
            usage_stage="single_codex",
            taskpack_id="SINGLE-CODEX-NONE",
        )
        invocation_context.update(
            _register_provider_launch(
                request,
                lifecycle_id="single-codex",
                workspace_root=request.project_root,
                usage_stage="single_codex",
                taskpack_id="SINGLE-CODEX-NONE",
            )
        )
        result = self.provider(
            project_root=request.project_root,
            prompt=prompt,
            model_policy=copy.deepcopy(request.model_policy),
            invocation_context=invocation_context,
        )
        if not isinstance(result, dict):
            raise ExperimentModeError(
                "single Codex provider returned an invalid result"
            )
        result = copy.deepcopy(result)
        result["provider_invocation_count"] = 1
        result["taskpack"] = None
        result["candidate_workspace"] = request.project_root
        return result


class NativeSingleCodexProvider:
    """One fresh live Codex call through the registered provider boundary."""

    def __init__(
        self,
        *,
        timeout_seconds=600,
    ):
        self.timeout_seconds = int(timeout_seconds)

    def __call__(
        self,
        *,
        project_root,
        prompt,
        model_policy,
        invocation_context,
    ):
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-C",
            str(project_root),
            "-s",
            model_policy["sandbox_policy"],
            "--json",
            "-m",
            model_policy["model"],
            "-c",
            (
                "model_reasoning_effort="
                f"{model_policy['reasoning_profile']}"
            ),
            "-",
        ]
        supported = is_supported_codex_command(command)
        if not supported:
            raise ExperimentModeError(
                "native single mode requires the Codex executable"
            )
        context = {
            **copy.deepcopy(invocation_context),
            "runtime_execution_session_id": (
                f"SINGLE-SESSION-{uuid.uuid4().hex}"
            ),
            "lifecycle_owner_token": (
                f"SINGLE-OWNER-{uuid.uuid4().hex}"
            ),
            "agent_id": "single-codex-controller",
            "role": "implementation_worker",
            "backend": "codex",
            "model": model_policy["model"],
            "reasoning_profile": model_policy[
                "reasoning_profile"
            ],
            "coverage_class": "supported_model_invocation",
        }
        invocation = ModelInvocationCall(
            context["model_invocation_authority_root"],
            context,
            supported=True,
        )
        execution = invocation.execute(
            command,
            cwd=project_root,
            input_text=prompt,
            timeout_seconds=self.timeout_seconds,
        )
        terminal_status = (
            "completed" if execution.returncode == 0 else "failed"
        )
        invocation.finalize(terminal_status, execution)
        return {
            "terminal_status": terminal_status,
            "adapter_output": {
                "returncode": execution.returncode,
                "invocation_id": invocation.lifecycle.invocation_id,
            },
        }


class AgentTeamDirectModeAdapter:
    """Run only the preregistered frozen taskpack on the trusted snapshot."""

    mode = "agentteam_direct"

    def __init__(self, frozen_taskpack_dir):
        self.frozen_taskpack_dir = str(
            Path(frozen_taskpack_dir).resolve(strict=True)
        )

    def preflight(self, request):
        expected = request.protocol["direct_taskpack"]["sha256"]
        snapshot_root = (
            Path(request.authority_root)
            / "direct-taskpack-snapshot"
        )
        try:
            if not snapshot_root.exists():
                staging = snapshot_root.with_name(
                    snapshot_root.name + f".{uuid.uuid4().hex}.tmp"
                )
                try:
                    shutil.copytree(
                        self.frozen_taskpack_dir,
                        staging,
                        symlinks=True,
                    )
                    verify_frozen_taskpack_digest(staging, expected)
                    staging.rename(snapshot_root)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
                _make_tree_read_only(snapshot_root)
            verified = verify_frozen_taskpack_digest(
                snapshot_root,
                expected,
            )
        except TaskpackValidationError as exc:
            raise ExperimentModeError(
                f"direct taskpack digest validation failed: {exc}"
            ) from exc
        return {
            **verified,
            "frozen_taskpack_dir": str(snapshot_root.resolve()),
        }

    def execute(self, request):
        verified = self.preflight(request)
        from .agentteam import _run_frozen_taskpack

        run_root = Path(request.run_dir) / "agentteam-runtime"
        launched = _run_frozen_taskpack(
            frozen_taskpack_dir=verified["frozen_taskpack_dir"],
            run_root=str(run_root),
            trusted_project_root=request.project_root,
            experiment_runtime_context=(
                _experiment_runtime_context(request)
            ),
        )
        result = _normalize_runtime_launcher_output(
            launched,
            mode="direct",
            run_root=run_root,
            taskpack_id=verified["taskpack_id"],
            fallback_workspace=request.project_root,
        )
        result["taskpack"] = {
            **verified,
            "source": "preregistered",
        }
        return result

    def failure_context(self, request):
        verified = self.preflight(request)
        run_root = Path(request.run_dir) / "agentteam-runtime"
        return {
            "candidate_workspace": (
                _integration_candidate_workspace(
                    run_root,
                    verified["taskpack_id"],
                )
                or request.project_root
            ),
            "taskpack": {
                **verified,
                "source": "preregistered",
            },
            "adapter_output": {},
        }


def _make_tree_read_only(root):
    root = Path(root)
    for path in sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_symlink():
            raise ExperimentModeError(
                "direct taskpack snapshot contains a symlink"
            )
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)


class AgentTeamFullModeAdapter:
    """Author, freeze, and execute a new taskpack without direct-taskpack input."""

    mode = "agentteam_full"

    def __init__(self):
        self._frozen_taskpack = None

    def preflight(self, _request):
        return None

    def execute(self, request):
        from .agentteam import _run_frozen_taskpack
        from .taskpack import freeze_taskpack
        from .taskpack_author import draft_taskpack_from_goal

        author_workspace, _branch = (
            create_independent_attempt_workspace(
                request.project_root,
                Path(request.run_dir) / "mode-workspaces",
                "AUTHOR",
                "WT-AUTHOR",
            )
        )
        draft_root = author_workspace / ".agentteam-author"
        draft_root.mkdir(mode=0o700)
        authored = draft_taskpack_from_goal(
            project_root=str(author_workspace),
            goal=_goal_text(request.protocol),
            draft_root=str(draft_root),
            taskpack_id="experiment-full-mode-author",
            author_runtime="codex",
            codex_model=request.model_policy["model"],
            author_invocation_context=_register_provider_launch(
                request,
                lifecycle_id="taskpack-author",
                workspace_root=str(author_workspace),
                usage_stage="taskpack_author",
                taskpack_id="experiment-full-mode-author",
            ),
        )
        if (
            not isinstance(authored, dict)
            or not authored.get("taskpack_dir")
        ):
            raise ExperimentModeError(
                "full AgentTeam author returned no taskpack"
            )
        frozen = freeze_taskpack(
            authored["taskpack_dir"],
            Path(request.run_dir) / "frozen",
            expected_authoring_mode=authored.get(
                "authoring_mode",
                "direct_draft",
            ),
        )
        if (
            not isinstance(frozen, dict)
            or not frozen.get("frozen_taskpack_dir")
            or not isinstance(frozen.get("manifest"), dict)
        ):
            raise ExperimentModeError(
                "full AgentTeam freeze returned invalid authority"
            )
        digest = frozen["manifest"].get("digest_sha256")
        if digest == request.protocol["direct_taskpack"]["sha256"]:
            raise ExperimentModeError(
                "full mode resolved to the preregistered direct taskpack"
            )
        self._frozen_taskpack = {
            "taskpack_id": frozen["manifest"].get("taskpack_id"),
            "digest_sha256": digest,
            "frozen_taskpack_dir": str(
                Path(frozen["frozen_taskpack_dir"]).resolve()
            ),
            "source": "authored",
        }
        run_root = Path(request.run_dir) / "agentteam-runtime"
        launched = _run_frozen_taskpack(
            frozen_taskpack_dir=frozen["frozen_taskpack_dir"],
            run_root=str(run_root),
            trusted_project_root=request.project_root,
            experiment_runtime_context=(
                _experiment_runtime_context(request)
            ),
        )
        result = _normalize_runtime_launcher_output(
            launched,
            mode="full",
            run_root=run_root,
            taskpack_id=frozen["manifest"].get("taskpack_id"),
            fallback_workspace=request.project_root,
        )
        result["taskpack"] = copy.deepcopy(self._frozen_taskpack)
        return result

    def failure_context(self, request):
        taskpack = copy.deepcopy(self._frozen_taskpack)
        taskpack_id = (
            taskpack.get("taskpack_id")
            if isinstance(taskpack, dict)
            else None
        )
        return {
            "candidate_workspace": (
                _integration_candidate_workspace(
                    Path(request.run_dir) / "agentteam-runtime",
                    taskpack_id,
                )
                or request.project_root
            ),
            "taskpack": taskpack,
            "adapter_output": {},
        }


def _normalize_runtime_launcher_output(
    value,
    *,
    mode,
    run_root,
    taskpack_id,
    fallback_workspace,
):
    if isinstance(value, dict):
        result = copy.deepcopy(value)
    else:
        returncode = getattr(value, "returncode", None)
        if not isinstance(returncode, int) or isinstance(
            returncode,
            bool,
        ):
            raise ExperimentModeError(
                f"{mode} AgentTeam runtime returned an invalid result"
            )
        result = {
            "terminal_status": (
                "completed" if returncode == 0 else "failed"
            ),
            "adapter_output": {
                "returncode": returncode,
                "stdout": str(
                    getattr(value, "stdout", "") or ""
                )[-4000:],
                "stderr": str(
                    getattr(value, "stderr", "") or ""
                )[-4000:],
            },
        }
    if not result.get("candidate_workspace"):
        result["candidate_workspace"] = (
            _integration_candidate_workspace(
                run_root,
                taskpack_id,
            )
            or str(Path(fallback_workspace).resolve(strict=True))
        )
    return result


def _normalize_mode_result(
    request,
    mode,
    result,
    *,
    authoritative_invocation_count,
):
    if not isinstance(result, dict):
        raise ExperimentModeError("mode adapter result must be an object")
    status = result.get("terminal_status") or result.get("status")
    if status not in _TERMINAL_STATUSES:
        raise ExperimentModeError(
            "mode adapter returned an invalid terminal status"
        )
    reported_invocation_count = result.get(
        "provider_invocation_count"
    )
    invocation_count = authoritative_invocation_count
    if (
        not isinstance(invocation_count, int)
        or isinstance(invocation_count, bool)
        or invocation_count < 0
    ):
        raise ExperimentModeError(
            "mode adapter omitted provider invocation count"
        )
    if (
        reported_invocation_count is not None
        and reported_invocation_count != invocation_count
    ):
        raise ExperimentModeError(
            "adapter invocation count differs from controller registry"
        )
    if (
        result.get(
            "common_contract_sha256",
            request.common_contract_sha256,
        )
        != request.common_contract_sha256
    ):
        raise ExperimentModeError(
            "mode adapter changed the common input contract"
        )
    return {
        "schema_version": MODE_RESULT_SCHEMA_VERSION,
        "experiment_run_id": request.run_manifest[
            "experiment_run_id"
        ],
        "protocol_sha256": request.run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(
            request.run_manifest
        ),
        "mode": mode,
        "terminal_status": status,
        "common_contract_sha256": request.common_contract_sha256,
        "provider_invocation_count": invocation_count,
        "project_root": request.project_root,
        "candidate_workspace": result.get("candidate_workspace"),
        "taskpack": copy.deepcopy(result.get("taskpack")),
        "retained_roots": copy.deepcopy(
            result.get("retained_roots", {})
        ),
        "adapter_output": copy.deepcopy(
            result.get("adapter_output", {})
        ),
    }


def _common_mode_contract(
    protocol,
    sandbox_configuration_sha256,
):
    return {
        "repository": copy.deepcopy(protocol["repository"]),
        "goal": copy.deepcopy(protocol["goal"]),
        "acceptance": copy.deepcopy(protocol["acceptance"]),
        "environment": copy.deepcopy(protocol["environment"]),
        "budgets": copy.deepcopy(protocol["budgets"]),
        "operator_limits": copy.deepcopy(protocol["operator_limits"]),
        "seed": protocol["seed"],
        "scored": protocol["scored"],
        "blind_gold": copy.deepcopy(protocol["blind_gold"]),
        "evaluator": copy.deepcopy(protocol["evaluator"]),
        "usage_contract_version": protocol["usage_contract_version"],
        "sandbox_configuration_sha256": (
            sandbox_configuration_sha256
        ),
    }


def _model_policy(protocol):
    environment = protocol["environment"]
    return {
        "backend": environment["backend"],
        "codex_cli_version": environment["codex_cli_version"],
        "model": environment["model"],
        "reasoning_profile": environment["reasoning_profile"],
        "service_configuration_sha256": environment[
            "service_configuration_sha256"
        ],
        "sandbox_policy": environment["sandbox_policy"],
        "permission_policy": environment["permission_policy"],
        "network_policy": environment["network_policy"],
        "tool_allowlist": copy.deepcopy(
            environment["tool_allowlist"]
        ),
        "max_inflight_model_invocations": environment[
            "max_inflight_model_invocations"
        ],
    }


def _single_codex_prompt(protocol):
    lines = [
        protocol["goal"]["summary"],
        "",
        "Constraints:",
        *(f"- {item}" for item in protocol["goal"]["constraints"]),
        "",
        "Acceptance command:",
        " ".join(protocol["acceptance"]["command"]),
    ]
    instructions = protocol["mode_instructions"]["single_codex"]
    if instructions:
        lines.extend(
            ["", "Mode instructions:", *(f"- {item}" for item in instructions)]
        )
    return "\n".join(lines)


def _goal_text(protocol):
    return "\n".join(
        [
            protocol["goal"]["summary"],
            *(f"- {item}" for item in protocol["goal"]["constraints"]),
        ]
    )


def _experiment_invocation_context(
    request,
    *,
    usage_stage,
    taskpack_id,
):
    return {
        "project": Path(request.project_root).name,
        "run_id": request.run_manifest["experiment_run_id"],
        "taskpack_id": taskpack_id,
        "usage_stage": usage_stage,
        "experiment_authority_root": request.authority_root,
        "experiment_controller_reference": copy.deepcopy(
            request.controller_reference
        ),
        "experiment_controller_required": True,
        "provider_resume_mode": "new",
    }


def _experiment_runtime_context(request, usage_stage=None):
    context = {
        "schema_version": "experiment_runtime_context.v1",
        "experiment_run_id": request.run_manifest[
            "experiment_run_id"
        ],
        "protocol_sha256": request.run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(
            request.run_manifest
        ),
        "authority_root": request.authority_root,
        "mode": request.run_manifest["mode"],
        "repository_identity": copy.deepcopy(
            request.protocol["repository"]
        ),
        "controller_reference": copy.deepcopy(
            request.controller_reference
        ),
        "controller_required": True,
        "independent_attempt_workspaces": True,
        "model_policy": copy.deepcopy(request.model_policy),
        "sandbox_configuration": copy.deepcopy(
            request.sandbox_configuration
        ),
        "sandbox_configuration_sha256": canonical_json_sha256(
            request.sandbox_configuration
        ),
    }
    if usage_stage is not None:
        context["usage_stage"] = usage_stage
    return context


def _register_provider_launch(
    request,
    *,
    lifecycle_id,
    workspace_root,
    usage_stage,
    taskpack_id,
):
    configuration = request.sandbox_configuration
    required = {
        "runtime_views",
        "library_views",
        "credential_mounts",
        "environment",
        "canary_path",
    }
    if not isinstance(configuration, dict) or set(configuration) != required:
        raise ExperimentModeError(
            "experiment sandbox configuration is invalid"
        )
    lifecycle_root = experiment_lifecycle_authority_root(
        request.authority_root,
        lifecycle_id,
    )
    descriptor = build_provider_sandbox_descriptor(
        workspace_root,
        runtime_views=configuration["runtime_views"],
        library_views=configuration["library_views"],
        credential_mounts=configuration["credential_mounts"],
        environment=configuration["environment"],
        repository_identity=_repository_identity(request.protocol),
        forbidden_paths=[configuration["canary_path"]],
    )
    sandbox_reference = publish_provider_sandbox_reference(
        request.authority_root,
        descriptor,
        configuration["canary_path"],
        reference_id=f"{lifecycle_id}-sandbox",
    )
    publish_experiment_launch_registration(
        request.authority_root,
        lifecycle_root,
        experiment_run_id=request.run_manifest["experiment_run_id"],
        protocol_sha256=request.run_manifest["protocol_sha256"],
        run_manifest_sha256=canonical_json_sha256(
            request.run_manifest
        ),
        mode=request.run_manifest["mode"],
        usage_stage=usage_stage,
        taskpack_id=taskpack_id,
        workspace_root=workspace_root,
        sandbox_reference=sandbox_reference,
        controller_reference=request.controller_reference,
        model_policy=request.model_policy,
    )
    return {
        **_experiment_invocation_context(
            request,
            usage_stage=usage_stage,
            taskpack_id=taskpack_id,
        ),
        "experiment_sandbox_reference": sandbox_reference,
        "experiment_sandbox_required": True,
        "model_invocation_authority_root": str(lifecycle_root),
        "model": request.model_policy["model"],
        "reasoning_profile": request.model_policy[
            "reasoning_profile"
        ],
    }


def _validate_sandbox_configuration(configuration):
    required = {
        "runtime_views",
        "library_views",
        "credential_mounts",
        "environment",
        "canary_path",
    }
    if not isinstance(configuration, dict) or set(configuration) != required:
        raise ExperimentModeError(
            "experiment sandbox configuration is invalid"
        )
    return configuration


def _repository_identity(protocol):
    repository = protocol["repository"]
    return {
        field: repository[field]
        for field in ("commit", "tree", "git_object_format")
    }


def _integration_candidate_workspace(run_root, taskpack_id):
    if not isinstance(taskpack_id, str) or not taskpack_id:
        return None
    state_path = (
        Path(run_root)
        / taskpack_id
        / "state"
        / "two_phase_scheduler_state.json"
    )
    if not state_path.is_file() or state_path.is_symlink():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    baseline = (
        state.get("integration_baseline")
        if isinstance(state, dict)
        else None
    )
    candidate = (
        baseline.get("integration_baseline_worktree_path")
        if isinstance(baseline, dict)
        else None
    )
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        return str(Path(candidate).resolve(strict=True))
    except OSError:
        return None


def _validated_candidate_workspace(request, mode_result):
    value = mode_result.get("candidate_workspace")
    if not isinstance(value, str) or not value:
        raise ExperimentModeError(
            "mode result omitted the candidate workspace"
        )
    candidate = Path(value).resolve(strict=True)
    run_dir = Path(request.run_dir).resolve(strict=True)
    try:
        candidate.relative_to(run_dir)
    except ValueError as exc:
        raise ExperimentModeError(
            "candidate workspace is outside the bound run"
        ) from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise ExperimentModeError(
            "candidate workspace is not a regular directory"
        )
    try:
        state = certify_candidate_repository(
            candidate,
            _repository_identity(request.protocol),
        )
    except Exception as exc:
        raise ExperimentModeError(
            "candidate workspace is not a standalone certified repository"
        ) from exc
    return candidate, state


def _validate_common_evaluation(
    request,
    evaluation,
    *,
    expected_path,
):
    if not isinstance(evaluation, dict):
        raise ExperimentModeError(
            "common evaluator returned an invalid result"
        )
    evaluation = copy.deepcopy(evaluation)
    required = {"evaluation_status", "evaluator_sha256"}
    if not required.issubset(evaluation):
        raise ExperimentModeError(
            "common evaluator result is incomplete"
        )
    if evaluation["evaluation_status"] not in {
        "passed",
        "failed",
        "blocked",
    }:
        raise ExperimentModeError(
            "common evaluator returned an invalid status"
        )
    if (
        evaluation["evaluator_sha256"]
        != request.protocol["evaluator"]["artifact_sha256"]
    ):
        raise ExperimentModeError(
            "common evaluator digest differs from protocol"
        )
    run_dir = Path(request.run_dir).resolve(strict=True)
    path = Path(expected_path).resolve(strict=True)
    try:
        relative_path = path.relative_to(run_dir)
    except ValueError as exc:
        raise ExperimentModeError(
            "common evaluation evidence is outside the bound run"
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise ExperimentModeError(
            "common evaluation evidence is not a regular file"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    reported_digest = evaluation.get("evaluation_sha256", digest)
    if reported_digest != digest:
        raise ExperimentModeError(
            "common evaluation evidence digest changed"
        )
    evaluation["evaluation_path"] = str(path)
    evaluation["evaluation_relative_path"] = str(relative_path)
    evaluation["evaluation_sha256"] = digest
    return evaluation


def _common_retained_roots(request, mode_result):
    run_dir = Path(request.run_dir).resolve(strict=True)
    defaults = {
        "prompt": run_dir / "retained" / "prompt",
        "context": run_dir / "retained" / "context",
        "taskpack": run_dir / "retained" / "taskpack",
        "artifacts": run_dir / "artifacts",
    }
    retained = {}
    supplied = mode_result.get("retained_roots")
    for group, default in defaults.items():
        default.mkdir(parents=True, mode=0o700, exist_ok=True)
        publish_immutable_json(
            default / "mode-input.json",
            {
                "schema_version": "experiment_retained_mode_input.v1",
                "experiment_run_id": request.run_manifest[
                    "experiment_run_id"
                ],
                "mode": request.run_manifest["mode"],
                "common_contract_sha256": (
                    request.common_contract_sha256
                ),
                "group": group,
            },
            label=f"retained {group} input",
        )
        paths = [str(default)]
        values = (
            supplied.get(group)
            if isinstance(supplied, dict)
            else None
        )
        if isinstance(values, (str, os.PathLike)):
            values = [values]
        if isinstance(values, (list, tuple)):
            for value in values:
                path = Path(value).resolve(strict=True)
                try:
                    path.relative_to(run_dir)
                except ValueError as exc:
                    raise ExperimentModeError(
                        f"retained {group} root is outside the bound run"
                    ) from exc
                if path.is_symlink():
                    raise ExperimentModeError(
                        f"retained {group} root is unsafe"
                    )
                paths.append(str(path))
        retained[group] = sorted(set(paths))
    return retained


def _build_common_result_bundle(
    request,
    mode_result,
    invocation_set_reference,
    evaluation,
    candidate_state,
    runtime_release_identity,
    retained_roots,
    scan_scope_reference,
):
    usage_totals, usage_coverage, terminal_statuses = (
        _registered_invocation_usage(
            invocation_set_reference,
            request.authority_root,
        )
    )
    controller = load_experiment_controller(
        request.controller_reference
    )
    operator_projection = controller.operator_action_projection(
        protocol=request.protocol,
        run_manifest=request.run_manifest,
    )
    budget = controller.snapshot()["budget_state"]
    metrics = measure_experiment_artifacts(
        request.run_dir,
        artifact_roots=retained_roots["artifacts"],
        raw_spool_roots=(),
    )
    changed_files = _candidate_changed_files(
        mode_result["candidate_workspace"],
        request.protocol["repository"]["commit"],
    )
    exhausted = bool(budget["exhausted"])
    budget_status = (
        "budget_stopped"
        if mode_result["terminal_status"] == "budget_stopped"
        else "exhausted"
        if exhausted
        else "within_budget"
    )
    regressions = []
    adapter_output = mode_result.get("adapter_output")
    failure_recovery = (
        adapter_output.get("failure_recovery")
        if isinstance(adapter_output, dict)
        else None
    )
    if isinstance(failure_recovery, dict):
        error_type = failure_recovery.get("error_type")
        error_sha256 = failure_recovery.get("error_sha256")
        if (
            isinstance(error_type, str)
            and error_type
            and isinstance(error_sha256, str)
            and len(error_sha256) == 64
            and all(
                character in "0123456789abcdef"
                for character in error_sha256
            )
        ):
            regressions.append(
                "infrastructure_failure:"
                f"{error_type}:{error_sha256}"
            )
    if evaluation["evaluation_status"] != "passed":
        regressions.append(
            evaluation.get("failure_reason") or "evaluation_failed"
        )
    return build_experiment_result_bundle(
        protocol=request.protocol,
        run_manifest=request.run_manifest,
        runtime_release_identity=runtime_release_identity,
        started_at=evaluation["started_at"],
        finished_at=evaluation["finished_at"],
        terminal_status=mode_result["terminal_status"],
        acceptance_result={
            "status": evaluation["evaluation_status"],
            "evaluation_sha256": evaluation["evaluation_sha256"],
        },
        usage_totals=usage_totals,
        usage_coverage=usage_coverage,
        budget_result={
            "status": budget_status,
            "max_total_tokens": budget["max_total_tokens"],
            "max_wall_time_seconds": budget[
                "max_wall_time_seconds"
            ],
            "total_tokens": budget["total_tokens"],
            "elapsed_wall_time_seconds": budget[
                "elapsed_wall_time_seconds"
            ],
            "overshoot_tokens": budget["overshoot_tokens"],
            "usage_complete": budget["usage_complete"],
            "exhausted": exhausted,
        },
        attempt_counts={
            "total": len(terminal_statuses),
            "accepted": sum(
                status == "completed"
                for status in terminal_statuses
            ),
        },
        verified_milestones=(
            ["common-evaluator"]
            if evaluation["evaluation_status"] == "passed"
            else []
        ),
        operator_action_counts=operator_projection[
            "operator_action_counts"
        ],
        retry_and_repair_counts={"retries": 0, "repairs": 0},
        changed_files=changed_files,
        regressions=sorted(set(regressions)),
        artifact_bytes_written=metrics["artifact_bytes_written"],
        raw_spool_bytes_written=metrics[
            "raw_spool_bytes_written"
        ],
        workspace_diff_sha256=canonical_json_sha256(
            candidate_state
        ),
        result_evidence={
            "evaluation_relative_path": evaluation[
                "evaluation_relative_path"
            ],
            "evaluation_sha256": evaluation["evaluation_sha256"],
            "taskpack_ids": evaluation["taskpack_ids"],
            "protocol_reference_sha256": evaluation[
                "experiment_protocol_reference_sha256"
            ],
            "invocation_set_reference_sha256": (
                invocation_set_reference["sha256"]
            ),
            "scan_scope_reference_sha256": (
                scan_scope_reference["sha256"]
            ),
            "scan_scope_sha256": evaluation[
                "scan_scope_sha256"
            ],
            "acceptance_command_sha256": evaluation[
                "acceptance_command_sha256"
            ],
            "acceptance_executable_sha256": evaluation[
                "acceptance_executable_sha256"
            ],
            "evaluator_sha256": evaluation["evaluator_sha256"],
            "provider_sandbox_reference_sha256": evaluation[
                "provider_sandbox_reference_sha256"
            ],
        },
        cleanup_status="pending",
    )


def _registered_invocation_usage(
    reference,
    authority_root,
    *,
    invocation_manifest=None,
):
    manifest = (
        invocation_manifest
        if invocation_manifest is not None
        else load_model_invocation_set_reference(
            reference,
            authority_root,
        )
    )
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    statuses = []
    covered = 0
    total = 0
    for item in manifest["invocation_sets"]:
        root = Path(item["lifecycle_authority_root"])
        for invocation_id in item["invocation_ids"]:
            terminal_path = (
                root
                / "model_invocations"
                / invocation_id
                / "terminal.json"
            )
            if terminal_path.is_symlink() or not terminal_path.is_file():
                raise ExperimentModeError(
                    "registered invocation terminal is unavailable"
                )
            terminal = json.loads(
                terminal_path.read_text(encoding="utf-8")
            )
            total += 1
            statuses.append(terminal["terminal_status"])
            if terminal.get("usage_status") == "reported":
                covered += 1
            for field in totals:
                value = terminal.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[field] += value
    coverage_status = (
        "complete"
        if covered == total
        else "partial"
        if covered
        else "unavailable"
    )
    return (
        totals,
        {
            "status": coverage_status,
            "covered_invocations": covered,
            "total_invocations": total,
        },
        statuses,
    )


def _reconcile_mode_invocations(request):
    from .two_phase_scheduler import reconcile_orphaned_invocation

    registry = (
        Path(request.authority_root) / "experiment_lifecycles"
    )
    if not registry.exists():
        return "no_provider_started"
    if registry.is_symlink() or not registry.is_dir():
        raise ExperimentModeError(
            "experiment lifecycle registry is unsafe"
        )
    durable_start_count = 0
    for lifecycle_root in sorted(registry.iterdir()):
        if lifecycle_root.is_symlink() or not lifecycle_root.is_dir():
            raise ExperimentModeError(
                "experiment lifecycle registry contains an unsafe entry"
            )
        invocation_root = lifecycle_root / "model_invocations"
        if not invocation_root.exists():
            continue
        for started_path in sorted(
            invocation_root.glob("*/started.json")
        ):
            if started_path.is_symlink() or not started_path.is_file():
                raise ExperimentModeError(
                    "experiment invocation start is unsafe"
                )
            durable_start_count += 1
            terminal_path = started_path.with_name("terminal.json")
            if terminal_path.is_file():
                continue
            start = json.loads(
                started_path.read_text(encoding="utf-8")
            )
            reconciliation = reconcile_orphaned_invocation(
                lifecycle_root,
                {
                    "attempt_id": start.get("attempt_id"),
                    "lease_id": start.get(
                        "lifecycle_owner_token"
                    ),
                    "agent_id": start.get("agent_id"),
                },
            )
            if reconciliation.get("reconciliation_status") not in {
                "terminal_available",
                "recovered",
            }:
                return (
                    "pending_fence:"
                    + str(
                        reconciliation.get(
                            "reconciliation_status"
                        )
                    )
                )
    if durable_start_count == 0:
        return "no_provider_started"
    controller = load_experiment_controller(
        request.controller_reference
    )
    try:
        controller.reconcile_provider_lane()
    except Exception as exc:
        return f"pending_fence:controller:{type(exc).__name__}"
    return "terminal_ready"


def _candidate_changed_files(candidate_workspace, baseline_commit):
    command = [
        "git",
        "-C",
        str(Path(candidate_workspace).resolve(strict=True)),
        "-c",
        "core.hooksPath=/dev/null",
        "diff",
        "--name-only",
        "-z",
        baseline_commit,
    ]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env=environment,
    )
    if completed.returncode != 0:
        raise ExperimentModeError(
            "candidate changed-file inventory is unavailable"
        )
    untracked = subprocess.run(
        [
            "git",
            "-C",
            str(Path(candidate_workspace).resolve(strict=True)),
            "-c",
            "core.hooksPath=/dev/null",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env=environment,
    )
    if untracked.returncode != 0:
        raise ExperimentModeError(
            "candidate untracked-file inventory is unavailable"
        )
    return sorted(
        {
            value.decode("utf-8", errors="surrogateescape")
            for value in (
                completed.stdout + untracked.stdout
            ).split(b"\0")
            if value
        }
    )


def _publish_mode_order_authority(controller_root, protocol):
    entries = []
    base_order = _seeded_base_mode_order(protocol)
    if protocol["mode_order"] != base_order:
        raise ExperimentModeError(
            "protocol mode order does not match its deterministic seed"
        )
    for repetition_index in range(
        protocol["repetition_policy"]["count"]
    ):
        shift = repetition_index % len(base_order)
        order = base_order[shift:] + base_order[:shift]
        entries.extend(
            {
                "sequence_index": len(entries),
                "repetition_index": repetition_index,
                "slot_index": slot_index,
                "mode": mode,
            }
            for slot_index, mode in enumerate(order)
        )
    return publish_immutable_json(
        Path(controller_root) / "experiment-mode-order.json",
        {
            "schema_version": "experiment_mode_order.v1",
            "protocol_sha256": canonical_json_sha256(protocol),
            "seed": protocol["seed"],
            "order_strategy": protocol["repetition_policy"][
                "order_strategy"
            ],
            "base_mode_order": base_order,
            "entries": entries,
        },
        label="experiment mode order",
    )


def _seeded_base_mode_order(protocol):
    return sorted(
        protocol["modes"],
        key=lambda mode: canonical_json_sha256(
            {"seed": protocol["seed"], "mode": mode}
        ),
    )


def _begin_mode_execution(controller_root, protocol, run_manifest):
    root = Path(controller_root).resolve(strict=True)
    authority = _load_mode_order_authority(root, protocol)
    entry = _mode_order_entry(authority, run_manifest)
    with _mode_order_lock(root):
        attempts = _mode_attempt_records(root, entry)
        if attempts:
            latest = attempts[-1]
            started_path = latest["started_path"]
            terminal_path = latest["terminal_path"]
            if not terminal_path.is_file():
                return {
                    **entry,
                    "attempt_index": latest["attempt_index"],
                    "execution_status": "interrupted",
                }
            terminal = json.loads(
                terminal_path.read_text(encoding="utf-8")
            )
            if terminal.get("experiment_run_id") != run_manifest[
                "experiment_run_id"
            ]:
                raise ExperimentModeError(
                    "mode sequence terminal belongs to another run"
                )
            if terminal.get("outcome") == "sealed":
                return {
                    **entry,
                    "attempt_index": latest["attempt_index"],
                    "execution_status": "already_completed",
                }
            if terminal.get("outcome") == "failed":
                return {
                    **entry,
                    "attempt_index": latest["attempt_index"],
                    "execution_status": "previously_failed",
                }
            if terminal.get("outcome") != (
                "retryable_infrastructure_failure"
            ):
                raise ExperimentModeError(
                    "mode sequence terminal outcome is invalid"
                )
            attempt_index = latest["attempt_index"] + 1
        else:
            attempt_index = 1
        for prior in authority["entries"][: entry["sequence_index"]]:
            if _completed_mode_entry_outcome(root, prior) not in {
                "sealed",
                "failed",
            }:
                raise ExperimentModeError(
                    "mode execution violates counterbalanced order"
                )
        started_path, _terminal_path = _mode_execution_paths(
            root,
            entry,
            attempt_index,
        )
        publish_immutable_json(
            started_path,
            {
                "schema_version": "experiment_mode_execution_start.v1",
                **entry,
                "experiment_run_id": run_manifest[
                    "experiment_run_id"
                ],
                "run_manifest_sha256": canonical_json_sha256(
                    run_manifest
                ),
                "attempt_index": attempt_index,
            },
            label="mode execution start",
        )
    return {
        **entry,
        "attempt_index": attempt_index,
        "execution_status": "started",
    }


def _complete_mode_execution(
    controller_root,
    protocol,
    run_manifest,
    *,
    sealed_result,
):
    root = Path(controller_root).resolve(strict=True)
    authority = _load_mode_order_authority(root, protocol)
    entry = _mode_order_entry(authority, run_manifest)
    with _mode_order_lock(root):
        attempt = _active_mode_attempt(root, entry)
        started_path = attempt["started_path"]
        terminal_path = attempt["terminal_path"]
        if not started_path.is_file():
            raise ExperimentModeError(
                "mode execution start authority is unavailable"
            )
        publish_immutable_json(
            terminal_path,
            {
                "schema_version": (
                    "experiment_mode_execution_terminal.v1"
                ),
                **entry,
                "experiment_run_id": run_manifest[
                    "experiment_run_id"
                ],
                "run_manifest_sha256": canonical_json_sha256(
                    run_manifest
                ),
                "attempt_index": attempt["attempt_index"],
                "outcome": "sealed",
                "bundle_sha256": sealed_result["bundle_sha256"],
                "terminal_status": sealed_result[
                    "terminal_status"
                ],
                "acceptance_status": sealed_result[
                    "acceptance_status"
                ],
            },
            label="mode execution terminal",
        )


def _fail_mode_execution(
    controller_root,
    protocol,
    run_manifest,
    *,
    run_dir,
    error,
):
    try:
        sealed = load_experiment_result_bundle(run_dir)
    except Exception:
        sealed = None
    if sealed is not None:
        _complete_mode_execution(
            controller_root,
            protocol,
            run_manifest,
            sealed_result={
                "bundle_sha256": sealed["bundle_sha256"],
                "terminal_status": sealed["bundle"][
                    "terminal_status"
                ],
                "acceptance_status": sealed["bundle"][
                    "acceptance_result"
                ]["status"],
            },
        )
        return
    root = Path(controller_root).resolve(strict=True)
    authority = _load_mode_order_authority(root, protocol)
    entry = _mode_order_entry(authority, run_manifest)
    with _mode_order_lock(root):
        attempt = _active_mode_attempt(root, entry)
        started_path = attempt["started_path"]
        terminal_path = attempt["terminal_path"]
        if not started_path.is_file():
            return
        publish_immutable_json(
            terminal_path,
            {
                "schema_version": (
                    "experiment_mode_execution_terminal.v1"
                ),
                **entry,
                "experiment_run_id": run_manifest[
                    "experiment_run_id"
                ],
                "run_manifest_sha256": canonical_json_sha256(
                    run_manifest
                ),
                "attempt_index": attempt["attempt_index"],
                "outcome": "retryable_infrastructure_failure",
                "error_type": type(error).__name__,
                "error_sha256": hashlib.sha256(
                    str(error).encode("utf-8")
                ).hexdigest(),
            },
            label="failed mode execution terminal",
        )


def _replayed_mode_result(run_dir, run_manifest):
    sealed = load_experiment_result_bundle(run_dir)
    bundle = sealed["bundle"]
    expected = {
        "experiment_run_id": run_manifest["experiment_run_id"],
        "protocol_sha256": run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "mode": run_manifest["mode"],
    }
    if any(bundle.get(field) != value for field, value in expected.items()):
        raise ExperimentModeError(
            "sealed replay result differs from immutable run authority"
        )
    cleanup = _ensure_sealed_result_cleanup(run_dir, sealed)
    return {
        "schema_version": MODE_RESULT_SCHEMA_VERSION,
        "experiment_run_id": run_manifest["experiment_run_id"],
        "protocol_sha256": run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "mode": run_manifest["mode"],
        "terminal_status": bundle["terminal_status"],
        "replayed": True,
        "sealed_result": {
            "result_dir": sealed["result_dir"],
            "bundle_sha256": sealed["bundle_sha256"],
            "terminal_status": bundle["terminal_status"],
            "acceptance_status": bundle["acceptance_result"]["status"],
            "cleanup_status": cleanup["receipt"]["cleanup_status"],
        },
        "cleanup_receipt": copy.deepcopy(cleanup),
    }


def _ensure_sealed_result_cleanup(run_dir, sealed):
    run_dir = Path(run_dir).resolve(strict=True)
    receipt_path = run_dir / CLEANUP_RECEIPT_FILE_NAME
    if receipt_path.exists() or receipt_path.is_symlink():
        return load_clean_snapshot_cleanup_receipt(
            run_dir,
            sealed_result=sealed,
        )
    cleanup_record = cleanup_clean_snapshot(
        run_dir,
        sealed_result_path=sealed["result_dir"],
    )
    return publish_clean_snapshot_cleanup_receipt(
        run_dir,
        sealed_result=sealed,
        cleanup_record=cleanup_record,
    )


def _load_mode_order_authority(controller_root, protocol):
    path = Path(controller_root) / "experiment-mode-order.json"
    if path.is_symlink() or not path.is_file():
        raise ExperimentModeError(
            "experiment mode order authority is unavailable"
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = _publish_mode_order_authority(
        controller_root,
        protocol,
    )
    if expected["sha256"] != canonical_json_sha256(value):
        raise ExperimentModeError(
            "experiment mode order authority changed"
        )
    return value


def _mode_order_entry(authority, run_manifest):
    matches = [
        entry
        for entry in authority["entries"]
        if entry["repetition_index"]
        == run_manifest["repetition_index"]
        and entry["mode"] == run_manifest["mode"]
    ]
    if len(matches) != 1:
        raise ExperimentModeError(
            "run is absent from the counterbalanced mode order"
        )
    return matches[0]


def _mode_execution_paths(controller_root, entry, attempt_index):
    prefix = (
        f"{entry['sequence_index']:04d}-"
        f"r{entry['repetition_index']:04d}-"
        f"{entry['mode']}-a{attempt_index:04d}"
    )
    directory = Path(controller_root) / "mode-executions"
    directory.mkdir(mode=0o700, exist_ok=True)
    return (
        directory / f"{prefix}.started.json",
        directory / f"{prefix}.terminal.json",
    )


def _mode_attempt_records(controller_root, entry):
    directory = Path(controller_root) / "mode-executions"
    if not directory.exists():
        return []
    prefix = (
        f"{entry['sequence_index']:04d}-"
        f"r{entry['repetition_index']:04d}-"
        f"{entry['mode']}-a"
    )
    records = []
    for started_path in sorted(
        directory.glob(f"{prefix}*.started.json")
    ):
        suffix = started_path.name[
            len(prefix): -len(".started.json")
        ]
        if len(suffix) != 4 or not suffix.isdigit():
            raise ExperimentModeError(
                "mode execution attempt name is invalid"
            )
        attempt_index = int(suffix)
        expected_started, terminal_path = _mode_execution_paths(
            controller_root,
            entry,
            attempt_index,
        )
        if expected_started != started_path:
            raise ExperimentModeError(
                "mode execution attempt path changed"
            )
        records.append(
            {
                "attempt_index": attempt_index,
                "started_path": started_path,
                "terminal_path": terminal_path,
            }
        )
    if [item["attempt_index"] for item in records] != list(
        range(1, len(records) + 1)
    ):
        raise ExperimentModeError(
            "mode execution attempt sequence is not contiguous"
        )
    return records


def _active_mode_attempt(controller_root, entry):
    attempts = _mode_attempt_records(controller_root, entry)
    if not attempts or attempts[-1]["terminal_path"].exists():
        raise ExperimentModeError(
            "mode execution has no active attempt"
        )
    return attempts[-1]


def _completed_mode_entry_outcome(controller_root, entry):
    attempts = _mode_attempt_records(controller_root, entry)
    if not attempts:
        return None
    terminal_path = attempts[-1]["terminal_path"]
    if not terminal_path.is_file():
        return None
    terminal = json.loads(
        terminal_path.read_text(encoding="utf-8")
    )
    return terminal.get("outcome")


@contextmanager
def _mode_order_lock(controller_root):
    path = Path(controller_root) / ".mode-order.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_sealed_mode_result(
    request,
    mode_result,
    invocation_set_reference,
    evaluation,
    sealed,
):
    bundle = sealed.get("bundle") if isinstance(sealed, dict) else None
    if not isinstance(bundle, dict):
        raise ExperimentModeError(
            "common result publisher produced no sealed bundle"
        )
    expected = {
        "experiment_run_id": request.run_manifest[
            "experiment_run_id"
        ],
        "protocol_sha256": request.run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(
            request.run_manifest
        ),
        "mode": request.run_manifest["mode"],
        "terminal_status": mode_result["terminal_status"],
    }
    for field, value in expected.items():
        if bundle.get(field) != value:
            raise ExperimentModeError(
                f"sealed result {field} differs from mode authority"
            )
    acceptance = bundle.get("acceptance_result", {})
    if (
        acceptance.get("status")
        != evaluation["evaluation_status"]
        or acceptance.get("evaluation_sha256")
        != evaluation["evaluation_sha256"]
    ):
        raise ExperimentModeError(
            "sealed acceptance result differs from common evaluator"
        )
    evidence = bundle.get("result_evidence", {})
    if (
        evidence.get("evaluation_relative_path")
        != evaluation["evaluation_relative_path"]
        or evidence.get("evaluation_sha256")
        != evaluation["evaluation_sha256"]
        or evidence.get("evaluator_sha256")
        != evaluation["evaluator_sha256"]
        or evidence.get("invocation_set_reference_sha256")
        != invocation_set_reference["sha256"]
    ):
        raise ExperimentModeError(
            "sealed result evidence differs from common authorities"
        )

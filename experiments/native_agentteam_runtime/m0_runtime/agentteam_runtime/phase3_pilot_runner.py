"""Resumable execution state machine for an admitted Phase 3B pilot."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .benchmark_adapter import validate_benchmark_instance_selection
from .benchmark_preregistration import validate_benchmark_preregistration
from .experiment_contract import (
    EXPERIMENT_MODES,
    PROTOCOL_SCHEMA_VERSION,
    allocate_experiment_run,
    canonical_json_sha256,
    publish_immutable_json,
    validate_experiment_protocol,
)
from .experiment_modes import (
    AgentTeamDirectModeAdapter,
    AgentTeamFullModeAdapter,
    ExperimentCommonFinalizer,
    NativeSingleCodexProvider,
    SingleCodexModeAdapter,
    execute_bound_experiment_mode,
)
from .experiment_results import load_experiment_result_bundle
from .phase3_pilot import (
    Phase3PilotError,
    admit_phase3_live_launch,
    validate_phase3_pilot_contract,
)
from .taskpack import draft_taskpack_files, freeze_taskpack
from .taskpack import BENCHMARK_REPOSITORY_WRITE_SCOPE


PILOT_RUN_MANIFEST_VERSION = "phase3_pilot_run_manifest.v1"
PILOT_RUN_STATE_VERSION = "phase3_pilot_run_state.v1"
_RESULT_STATUSES = {
    "completed",
    "failed",
    "infrastructure_failed",
    "budget_stopped",
}
_RETRYABLE_FAILURES = {
    "provider_transport_error",
    "provider_rate_limit",
}
_FORBIDDEN_RESULT_KEYS = {
    "gold_patch",
    "test_patch",
    "fail_to_pass",
    "pass_to_pass",
    "all_patch",
}


class Phase3PilotRunnerError(Phase3PilotError):
    """Raised when scored pilot execution cannot continue safely."""


def build_phase3_experiment_protocol(
    *,
    instance_id,
    preregistration,
    repository_source,
    common_evaluator_artifact,
):
    """Translate one frozen benchmark authority into executable protocol v1."""

    preregistration = validate_benchmark_preregistration(preregistration)
    authority = preregistration["authorization"]
    if authority["selection"]["ordered_instance_ids"] != [instance_id]:
        raise Phase3PilotRunnerError(
            "protocol instance does not match preregistration"
        )
    visible = authority["equal_input_bindings"]["shared_visible_inputs"]
    source = Path(repository_source).resolve(strict=True)
    repository = copy.deepcopy(visible["repository"])
    _verify_repository_mirror(source, repository)
    repository["source"] = str(source)
    task = visible["task"]
    command = _absolute_argv(task["acceptance_commands"][0])
    evaluator = Path(common_evaluator_artifact).resolve(strict=True)
    if evaluator.is_symlink() or not evaluator.is_file():
        raise Phase3PilotRunnerError("common evaluator artifact is unsafe")
    budget = authority["budgets"]["single_codex"]
    direct_digest = authority["mode_controls"]["agentteam_direct"][
        "taskpack_sha256_by_instance"
    ][instance_id]
    execution = visible["execution"]
    model = visible["model"]
    runtime = visible["runtime"]
    protocol = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "experiment_id": "phase3b-" + _compact_id(instance_id),
        "instance_id": instance_id,
        "repository": repository,
        "goal": {
            "summary": task["goal"],
            "constraints": [*task["constraints"], *task["non_goals"]],
        },
        "acceptance": {
            "command": command,
            "timeout_seconds": budget["max_wall_time_seconds"],
        },
        "modes": list(EXPERIMENT_MODES),
        "mode_order": list(authority["repetition_policy"]["mode_order"][0]),
        "mode_instructions": {
            "single_codex": ["Solve the frozen public task directly."],
            "agentteam_direct": ["Use only the preregistered taskpack."],
            "agentteam_full": ["Author and freeze a fresh taskpack from the public goal."],
        },
        "repetition_policy": {
            "count": authority["repetition_policy"]["max_repetitions"],
            "order_strategy": "seeded_counterbalanced_rotation",
        },
        "environment": {
            "backend": "codex",
            "codex_cli_version": runtime["codex_cli_version"],
            "model": model["model"],
            "reasoning_profile": model["reasoning_profile"],
            "service_configuration_sha256": model[
                "service_configuration_sha256"
            ],
            "sandbox_policy": "workspace-write",
            "permission_policy": "never",
            "network_policy": "provider_access",
            "tool_allowlist": ["exec_command", "apply_patch"],
            "host_class": execution["host_class"],
            "cpu_limit": execution["cpu_limit"],
            "memory_limit_bytes": execution["memory_limit_bytes"],
            "dependency_cache_policy": "declared_equal_read_only",
            "max_inflight_model_invocations": 1,
        },
        "seed": 0,
        "scored": True,
        "blind_gold": {"policy": "unavailable_to_runtime"},
        "budgets": {
            "max_total_tokens": budget["max_total_tokens"],
            "max_wall_time_seconds": budget["max_wall_time_seconds"],
            "soft_warning_ratio": 0.8,
            "stop_boundaries": [
                "pre_provider_launch",
                "post_invocation_terminal",
                "pre_integration",
                "post_integration",
            ],
        },
        "operator_limits": {
            "expected_operator_action": 0,
            "corrective_intervention": 0,
            "decision_escalation": 0,
        },
        "evaluator": {
            "version": "phase3-visible-acceptance.v1",
            "artifact_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        },
        "direct_taskpack": {
            "sha256": direct_digest,
            "cost_reported_separately": True,
            "available_to_full_mode": False,
        },
        "usage_contract_version": "model_invocation_usage.v1",
    }
    validate_experiment_protocol(protocol)
    return protocol


def materialize_phase3_runtime_taskpack(
    *,
    instance_id,
    benchmark_taskpack,
    project_root,
    output_root,
    model,
):
    """Build the runtime taskpack paired with one benchmark semantic authority."""

    if (
        not isinstance(benchmark_taskpack, dict)
        or benchmark_taskpack.get("schema_version") != "phase3_direct_taskpack.v1"
        or benchmark_taskpack.get("taskpack", {}).get("instance_id") != instance_id
    ):
        raise Phase3PilotRunnerError("benchmark direct taskpack is invalid")
    semantic_digest = benchmark_taskpack.get("taskpack_sha256")
    if semantic_digest != canonical_json_sha256(benchmark_taskpack["taskpack"]):
        raise Phase3PilotRunnerError("benchmark direct taskpack digest changed")
    project_root = Path(project_root).resolve(strict=True)
    output_root = Path(output_root).resolve()
    draft_root = output_root / "draft"
    frozen_root = output_root / "frozen"
    task = benchmark_taskpack["taskpack"]
    task_id = task["taskpack_id"]
    draft_path = draft_root / task_id
    frozen_path = frozen_root / task_id
    if frozen_path.is_dir():
        manifest = _read_json(frozen_path / "manifest.json", "runtime taskpack manifest")
        return {
            "semantic_authority_sha256": semantic_digest,
            "runtime_taskpack_sha256": manifest["digest_sha256"],
            "frozen_taskpack_dir": str(frozen_path),
        }
    if draft_path.exists():
        shutil.rmtree(draft_path)
    built = draft_taskpack_files(
        project_root=project_root,
        goal=task["tasks"][0]["objective"],
        draft_root=draft_root,
        taskpack_id=task_id,
        read_scope=["."],
        write_scope=["benchmark-candidate-placeholder/"],
        verification_command=_taskpack_acceptance_argv(
            task["tasks"][0]["acceptance_commands"][0]
        ),
        codex_timeout_seconds=task["shared_budget"]["max_wall_time_seconds"],
        codex_model=model,
        role_routing=False,
        risk_target="L1",
    )
    draft_directory = Path(built["taskpack_dir"])
    taskpack_payload = _read_json(
        draft_directory / "taskpack.yaml",
        "runtime taskpack",
    )
    backlog_payload = _read_json(
        draft_directory / "backlog.json",
        "runtime taskpack backlog",
    )
    taskpack_payload["execution_mode"] = "benchmark_experiment"
    for item in backlog_payload["items"]:
        item["write_scope"] = [BENCHMARK_REPOSITORY_WRITE_SCOPE]
    _write_json_file(draft_directory / "taskpack.yaml", taskpack_payload)
    _write_json_file(draft_directory / "backlog.json", backlog_payload)
    frozen = freeze_taskpack(
        built["taskpack_dir"],
        frozen_root,
        expected_authoring_mode="direct_draft",
    )
    return {
        "semantic_authority_sha256": semantic_digest,
        "runtime_taskpack_sha256": frozen["manifest"]["digest_sha256"],
        "frozen_taskpack_dir": frozen["frozen_taskpack_dir"],
    }


class Phase3ProductionExecutor:
    """Bridge pilot entries to existing mode execution and official scoring."""

    def __init__(
        self,
        *,
        experiment_root,
        protocols_by_instance,
        runtime_release,
        runtime_taskpacks_by_instance,
        sandbox_configuration,
        common_evaluator_artifact,
        official_evaluator,
        integration_verification_command=None,
        resource_envelope_binding=None,
        resource_hierarchy_references=None,
    ):
        self.root = Path(experiment_root).resolve()
        self.protocols = copy.deepcopy(protocols_by_instance)
        self.runtime_release = copy.deepcopy(runtime_release)
        self.taskpacks = copy.deepcopy(runtime_taskpacks_by_instance)
        self.sandbox_configuration = copy.deepcopy(sandbox_configuration)
        self.common_evaluator_artifact = str(
            Path(common_evaluator_artifact).resolve(strict=True)
        )
        if not callable(official_evaluator):
            raise Phase3PilotRunnerError("official evaluator must be callable")
        self.official_evaluator = official_evaluator
        self.integration_verification_command = copy.deepcopy(
            integration_verification_command
        )
        self.resource_envelope_binding = copy.deepcopy(resource_envelope_binding)
        self.resource_references = copy.deepcopy(
            resource_hierarchy_references or {}
        )
        for protocol in self.protocols.values():
            validate_experiment_protocol(protocol)

    def execute(self, entry, attempt_index):
        allocation = self._allocation(entry, attempt_index)
        run_dir = Path(allocation["run_dir"])
        official_path = run_dir / "results" / "official-score.json"
        if official_path.is_file():
            return self._terminal_result(entry, run_dir, _read_json(official_path, "official score"))
        adapter = self._adapter(entry)
        execute_bound_experiment_mode(
            allocation,
            sandbox_configuration=self.sandbox_configuration,
            adapter=adapter,
            common_finalizer=ExperimentCommonFinalizer(
                evaluator_artifact=self.common_evaluator_artifact,
                runtime_release_identity=self.runtime_release,
            ),
            resource_envelope_binding=self.resource_envelope_binding,
            resource_hierarchy_reference=self.resource_references.get(entry["mode"]),
        )
        score = self._evaluate_official(entry, run_dir)
        score = _validate_official_score(score)
        publish_immutable_json(
            official_path,
            score,
            label="Phase 3 official score",
        )
        return self._terminal_result(entry, run_dir, score)

    def recover(self, entry, _attempt_index=0):
        run_dir = self._run_dir(entry)
        official_path = run_dir / "results" / "official-score.json"
        if official_path.is_file():
            return self._terminal_result(
                entry,
                run_dir,
                _read_json(official_path, "official score"),
            )
        terminal = run_dir / "results" / "terminal"
        patch = run_dir / "artifacts" / "candidate.patch"
        if terminal.is_dir() and patch.is_file():
            score = self._evaluate_official(entry, run_dir)
            publish_immutable_json(
                official_path,
                score,
                label="Phase 3 official score",
            )
            return self._terminal_result(entry, run_dir, score)
        return None

    def _evaluate_official(self, entry, run_dir):
        patch = run_dir / "artifacts" / "candidate.patch"
        bound = getattr(self.official_evaluator, "evaluate_resource_bound", None)
        if self.resource_envelope_binding is not None:
            reference = self.resource_references.get(entry["mode"])
            if not callable(bound) or not isinstance(reference, dict):
                raise Phase3PilotRunnerError(
                    "official evaluator resource binding is incomplete"
                )
            score = bound(
                copy.deepcopy(entry),
                patch,
                resource_envelope_binding=self.resource_envelope_binding,
                resource_hierarchy_reference=reference,
                evidence_path=(
                    run_dir / "results" / "official-evaluator-resource.json"
                ),
            )
        else:
            score = self.official_evaluator(copy.deepcopy(entry), patch)
        return _validate_official_score(score)

    def _allocation(self, entry, attempt_index):
        protocol = self.protocols[entry["instance_id"]]
        return allocate_experiment_run(
            self.root / entry["instance_id"],
            protocol,
            mode=entry["mode"],
            repetition_index=entry["repetition_index"],
            stable_request_key=self._stable_key(entry),
            runtime_release=self.runtime_release,
        )

    def _run_dir(self, entry):
        allocation = self._allocation(entry, 0)
        return Path(allocation["run_dir"])

    @staticmethod
    def _stable_key(entry):
        return _compact_id(entry["entry_id"])

    def _adapter(self, entry):
        mode = entry["mode"]
        if mode == "single_codex":
            return SingleCodexModeAdapter(
                provider=NativeSingleCodexProvider(
                    timeout_seconds=self.protocols[entry["instance_id"]][
                        "budgets"
                    ]["max_wall_time_seconds"]
                )
            )
        if mode == "agentteam_full":
            return AgentTeamFullModeAdapter(
                integration_verification_command=self.integration_verification_command
            )
        taskpack = self.taskpacks[entry["instance_id"]]
        return AgentTeamDirectModeAdapter(
            taskpack["frozen_taskpack_dir"],
            semantic_authority_sha256=taskpack["semantic_authority_sha256"],
            runtime_taskpack_sha256=taskpack["runtime_taskpack_sha256"],
            integration_verification_command=self.integration_verification_command,
        )

    def _terminal_result(self, entry, run_dir, score):
        sealed = load_experiment_result_bundle(run_dir)["bundle"]
        usage = sealed["usage_totals"]
        return {
            "entry_id": entry["entry_id"],
            "terminal_status": sealed["terminal_status"],
            "failure_class": None,
            "usage": {
                field: int(usage.get(field, 0))
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                )
            }
            | {"coverage_percent": int(sealed["usage_coverage"]["coverage_percent"])},
            "wall_time_seconds": (
                float(sealed["budget_result"]["elapsed_wall_time_seconds"])
                + float(score["wall_time_seconds"])
            ),
            "official_score": copy.deepcopy(score),
            "mode_result_bundle_sha256": canonical_json_sha256(sealed),
        }


class Phase3PilotRunner:
    """Execute one admitted pilot serially and resume from sealed outcomes.

    ``executor`` owns the production mode/evaluator bridge. It must expose
    ``execute(entry, attempt_index)`` and may expose ``recover(entry)``. Both
    return the same bounded terminal result object.
    """

    def __init__(
        self,
        pilot_root,
        *,
        pilot_id,
        pilot_contract,
        live_authorization,
        selection,
        preregistrations_by_instance,
        expected_epoch_number,
        expected_epoch_sha256,
        executor,
    ):
        self.root = Path(pilot_root).resolve()
        self.pilot_id = _safe_id(pilot_id, "pilot_id")
        self.pilot_contract = copy.deepcopy(pilot_contract)
        self.live_authorization = copy.deepcopy(live_authorization)
        self.selection = copy.deepcopy(selection)
        self.preregistrations = copy.deepcopy(preregistrations_by_instance)
        self.expected_epoch_number = expected_epoch_number
        self.expected_epoch_sha256 = expected_epoch_sha256
        if not callable(getattr(executor, "execute", None)):
            raise Phase3PilotRunnerError("pilot executor must define execute")
        self.executor = executor
        self._validate_authorities()

    @property
    def manifest_path(self):
        return self.root / "pilot-manifest.json"

    @property
    def state_path(self):
        return self.root / "pilot-state.json"

    def initialize(self):
        """Publish immutable identity and create or validate the checkpoint."""

        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        with self._lease():
            manifest = self._expected_manifest()
            publish_immutable_json(
                self.manifest_path,
                manifest,
                label="Phase 3 pilot run manifest",
            )
            if self.state_path.exists() or self.state_path.is_symlink():
                state = self._load_state()
            else:
                state = self._new_state(manifest)
                self._write_state(state)
            self._validate_state(state, manifest)
            return copy.deepcopy(state)

    def run(self, *, max_executions=None):
        """Run scheduled entries until complete, stopped, or the step bound."""

        if max_executions is not None and (
            not isinstance(max_executions, int) or max_executions < 1
        ):
            raise Phase3PilotRunnerError("max_executions must be a positive integer")
        self.initialize()
        executions = 0
        while max_executions is None or executions < max_executions:
            with self._lease():
                self._validate_authorities()
                manifest = self._load_manifest()
                state = self._load_state()
                self._validate_state(state, manifest)
                if state["status"] in {"completed", "stopped"}:
                    return copy.deepcopy(state)
                recovered = self._recover_active(state)
                if recovered:
                    self._write_state(state)
                    continue
                entry = self._next_entry(state)
                if entry is None:
                    state["status"] = "completed"
                    state["active"] = None
                    state["finished_at"] = _utc_now()
                    self._write_state(state)
                    return copy.deepcopy(state)
                self._require_budget_available(state, entry)
                attempt_index = len(state["attempts"].get(entry["entry_id"], []))
                state["active"] = {
                    "entry_id": entry["entry_id"],
                    "attempt_index": attempt_index,
                    "started_at": _utc_now(),
                }
                state["updated_at"] = _utc_now()
                self._write_state(state)

            try:
                result = self.executor.execute(copy.deepcopy(entry), attempt_index)
            except Exception as exc:
                with self._lease():
                    state = self._load_state()
                    active = state.get("active")
                    if not isinstance(active, dict) or active.get("entry_id") != entry[
                        "entry_id"
                    ]:
                        raise Phase3PilotRunnerError(
                            "pilot active checkpoint changed during execution"
                        ) from exc
                    state["status"] = "stopped"
                    state["stop_reason"] = "executor_exception"
                    state["active_error"] = {
                        "type": type(exc).__name__,
                        "message_sha256": canonical_json_sha256(str(exc)),
                    }
                    state["updated_at"] = _utc_now()
                    self._write_state(state)
                raise Phase3PilotRunnerError("pilot executor raised") from exc

            with self._lease():
                state = self._load_state()
                self._accept_result(state, entry, attempt_index, result)
                self._write_state(state)
            executions += 1
        return self.status()

    def status(self):
        with self._lease():
            manifest = self._load_manifest()
            state = self._load_state()
            self._validate_state(state, manifest)
            return copy.deepcopy(state)

    def _validate_authorities(self):
        validate_benchmark_instance_selection(self.selection)
        for preregistration in self.preregistrations.values():
            validate_benchmark_preregistration(preregistration)
        self.pilot_contract = validate_phase3_pilot_contract(
            self.pilot_contract,
            selection=self.selection,
            preregistrations_by_instance=self.preregistrations,
        )
        self.permit = admit_phase3_live_launch(
            self.pilot_contract,
            self.live_authorization,
            selection=self.selection,
            preregistrations_by_instance=self.preregistrations,
            expected_epoch_number=self.expected_epoch_number,
            expected_epoch_sha256=self.expected_epoch_sha256,
        )

    def _expected_manifest(self):
        body = self.pilot_contract["contract"]
        preregistration_digests = {
            instance_id: self.preregistrations[instance_id]["authorization_sha256"]
            for instance_id in body["selection"]["ordered_instance_ids"]
        }
        manifest = {
            "schema_version": PILOT_RUN_MANIFEST_VERSION,
            "pilot_id": self.pilot_id,
            "pilot_contract_sha256": self.pilot_contract["contract_sha256"],
            "authorization_sha256": self.permit["authorization_sha256"],
            "epoch_number": self.permit["epoch_number"],
            "selection_sha256": body["selection"]["selection_sha256"],
            "ordered_instance_ids": body["selection"]["ordered_instance_ids"],
            "preregistration_authorization_sha256_by_instance": (
                preregistration_digests
            ),
            "modes": body["modes"],
            "mode_order": body["mode_order"],
            "aggregate_budget_ceiling": {
                "maximum_total_tokens": self.permit["maximum_total_tokens"],
                "maximum_wall_time_seconds": self.permit[
                    "maximum_wall_time_seconds"
                ],
                "max_inflight_model_invocations": self.permit[
                    "max_inflight_model_invocations"
                ],
            },
            "retry_policy": body["retry_policy"],
        }
        return manifest

    def _new_state(self, manifest):
        schedule = self._initial_schedule(manifest)
        now = _utc_now()
        return {
            "schema_version": PILOT_RUN_STATE_VERSION,
            "pilot_id": self.pilot_id,
            "manifest_sha256": canonical_json_sha256(manifest),
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "stop_reason": None,
            "active": None,
            "schedule": schedule,
            "third_repetition_planned": False,
            "attempts": {},
            "terminal_results": {},
            "aggregate_usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "wall_time_seconds": 0.0,
            },
            "usage_by_instance": {
                instance_id: {
                    "total_tokens": 0,
                    "wall_time_seconds": 0.0,
                }
                for instance_id in manifest["ordered_instance_ids"]
            },
        }

    def _initial_schedule(self, manifest):
        schedule = []
        for instance_id in manifest["ordered_instance_ids"]:
            for repetition_index in range(2):
                for mode in manifest["mode_order"][repetition_index]:
                    schedule.append(
                        _schedule_entry(instance_id, mode, repetition_index)
                    )
        return schedule

    def _next_entry(self, state):
        for entry in state["schedule"]:
            if entry["entry_id"] not in state["terminal_results"]:
                return entry
        if not state["third_repetition_planned"]:
            state["schedule"].extend(self._third_repetition_schedule(state))
            state["third_repetition_planned"] = True
            for entry in state["schedule"]:
                if entry["entry_id"] not in state["terminal_results"]:
                    return entry
        return None

    def _third_repetition_schedule(self, state):
        order = self.pilot_contract["contract"]["mode_order"][2]
        entries = []
        for instance_id in self.pilot_contract["contract"]["selection"][
            "ordered_instance_ids"
        ]:
            triggered = {
                mode
                for mode in EXPERIMENT_MODES
                if self._third_repetition_required(state, instance_id, mode)
            }
            for mode in order:
                if mode in triggered:
                    entries.append(_schedule_entry(instance_id, mode, 2))
        return entries

    def _third_repetition_required(self, state, instance_id, mode):
        results = [
            state["terminal_results"].get(
                _schedule_entry(instance_id, mode, repetition)["entry_id"]
            )
            for repetition in (0, 1)
        ]
        if any(not isinstance(result, dict) for result in results):
            raise Phase3PilotRunnerError("initial repetition result is missing")
        outcomes = [result["official_score"]["resolved"] for result in results]
        if outcomes[0] != outcomes[1]:
            return True
        threshold = self.preregistrations[instance_id]["authorization"][
            "thresholds"
        ]["third_repetition_variance_percent"]
        for field in ("total_tokens", "wall_time_seconds"):
            values = [
                result["usage"][field]
                if field == "total_tokens"
                else result["wall_time_seconds"]
                for result in results
            ]
            if _variance_percent(values) > threshold:
                return True
        return False

    def _recover_active(self, state):
        active = state.get("active")
        if not isinstance(active, dict):
            return False
        entry = next(
            (
                item
                for item in state["schedule"]
                if item["entry_id"] == active.get("entry_id")
            ),
            None,
        )
        if entry is None:
            raise Phase3PilotRunnerError("active pilot entry is absent from schedule")
        recover = getattr(self.executor, "recover", None)
        if not callable(recover):
            raise Phase3PilotRunnerError(
                "ambiguous active execution cannot resume without recovery"
            )
        result = recover(copy.deepcopy(entry), active["attempt_index"])
        if result is None:
            raise Phase3PilotRunnerError(
                "active execution has no sealed result; duplicate launch forbidden"
            )
        self._accept_result(state, entry, active["attempt_index"], result)
        return True

    def _accept_result(self, state, entry, attempt_index, result):
        result = _validate_terminal_result(result, entry)
        attempt = {
            "attempt_index": attempt_index,
            "terminal_status": result["terminal_status"],
            "failure_class": result.get("failure_class"),
            "usage": copy.deepcopy(result["usage"]),
            "wall_time_seconds": result["wall_time_seconds"],
            "result_sha256": canonical_json_sha256(result),
        }
        state["attempts"].setdefault(entry["entry_id"], []).append(attempt)
        self._add_usage(state, entry, result)
        state["active"] = None
        state["updated_at"] = _utc_now()
        failure_class = result.get("failure_class")
        retry_limit = self.pilot_contract["contract"]["retry_policy"][
            "provider_retry_limit"
        ]
        contract_retryable = set(
            self.pilot_contract["contract"]["retry_policy"][
                "retryable_failures"
            ]
        )
        if failure_class in contract_retryable and attempt_index < retry_limit:
            return
        state["terminal_results"][entry["entry_id"]] = copy.deepcopy(result)
        if result["usage"]["coverage_percent"] != 100:
            state["status"] = "stopped"
            state["stop_reason"] = "incomplete_provider_usage"
        ceiling = self.permit
        if state["aggregate_usage"]["total_tokens"] > ceiling[
            "maximum_total_tokens"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "aggregate_token_budget_exhausted"
        if state["aggregate_usage"]["wall_time_seconds"] > ceiling[
            "maximum_wall_time_seconds"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "aggregate_wall_budget_exhausted"
        instance_binding = next(
            binding
            for binding in self.pilot_contract["contract"]["instance_bindings"]
            if binding["instance_id"] == entry["instance_id"]
        )
        instance_usage = state["usage_by_instance"][entry["instance_id"]]
        if instance_usage["total_tokens"] > instance_binding[
            "maximum_total_tokens"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "instance_token_budget_exhausted"
        if instance_usage["wall_time_seconds"] > instance_binding[
            "maximum_wall_time_seconds"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "instance_wall_budget_exhausted"

    def _add_usage(self, state, entry, result):
        aggregate = state["aggregate_usage"]
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            aggregate[field] += result["usage"][field]
        aggregate["wall_time_seconds"] += result["wall_time_seconds"]
        instance = state["usage_by_instance"][entry["instance_id"]]
        instance["total_tokens"] += result["usage"]["total_tokens"]
        instance["wall_time_seconds"] += result["wall_time_seconds"]

    def _require_budget_available(self, state, entry):
        usage = state["aggregate_usage"]
        if usage["total_tokens"] >= self.permit["maximum_total_tokens"]:
            state["status"] = "stopped"
            state["stop_reason"] = "aggregate_token_budget_exhausted"
            self._write_state(state)
            raise Phase3PilotRunnerError("aggregate token budget is exhausted")
        if usage["wall_time_seconds"] >= self.permit[
            "maximum_wall_time_seconds"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "aggregate_wall_budget_exhausted"
            self._write_state(state)
            raise Phase3PilotRunnerError("aggregate wall budget is exhausted")
        instance_binding = next(
            binding
            for binding in self.pilot_contract["contract"]["instance_bindings"]
            if binding["instance_id"] == entry["instance_id"]
        )
        instance_usage = state["usage_by_instance"][entry["instance_id"]]
        if instance_usage["total_tokens"] >= instance_binding["maximum_total_tokens"]:
            state["status"] = "stopped"
            state["stop_reason"] = "instance_token_budget_exhausted"
            self._write_state(state)
            raise Phase3PilotRunnerError("instance token budget is exhausted")
        if instance_usage["wall_time_seconds"] >= instance_binding[
            "maximum_wall_time_seconds"
        ]:
            state["status"] = "stopped"
            state["stop_reason"] = "instance_wall_budget_exhausted"
            self._write_state(state)
            raise Phase3PilotRunnerError("instance wall budget is exhausted")

    def _load_manifest(self):
        value = _read_json(self.manifest_path, "pilot manifest")
        expected = self._expected_manifest()
        if value != expected:
            raise Phase3PilotRunnerError("pilot manifest authority drifted")
        return value

    def _load_state(self):
        return _read_json(self.state_path, "pilot state")

    def _validate_state(self, state, manifest):
        if not isinstance(state, dict) or state.get("schema_version") != (
            PILOT_RUN_STATE_VERSION
        ):
            raise Phase3PilotRunnerError("pilot state is invalid")
        if state.get("pilot_id") != self.pilot_id or state.get(
            "manifest_sha256"
        ) != canonical_json_sha256(manifest):
            raise Phase3PilotRunnerError("pilot state does not bind its manifest")
        if state.get("status") not in {"running", "completed", "stopped"}:
            raise Phase3PilotRunnerError("pilot state status is invalid")
        schedule = state.get("schedule")
        if not isinstance(schedule, list) or len(
            {item.get("entry_id") for item in schedule if isinstance(item, dict)}
        ) != len(schedule):
            raise Phase3PilotRunnerError("pilot schedule is invalid")
        initial = self._initial_schedule(manifest)
        if schedule[: len(initial)] != initial:
            raise Phase3PilotRunnerError("pilot initial schedule drifted")
        expected_third = (
            self._third_repetition_schedule(state)
            if state.get("third_repetition_planned")
            else []
        )
        if schedule[len(initial) :] != expected_third:
            raise Phase3PilotRunnerError("pilot third repetition schedule drifted")
        return state

    def _write_state(self, state):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = json.dumps(
            state,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        ) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".pilot-state-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @contextmanager
    def _lease(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = self.root / ".pilot.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Phase3PilotRunnerError(
                    "another Phase 3 pilot controller owns the lease"
                ) from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _schedule_entry(instance_id, mode, repetition_index):
    if mode not in EXPERIMENT_MODES:
        raise Phase3PilotRunnerError("pilot mode is invalid")
    entry_id = f"{instance_id}--r{repetition_index}--{mode}"
    return {
        "entry_id": entry_id,
        "instance_id": instance_id,
        "mode": mode,
        "repetition_index": repetition_index,
    }


def _validate_terminal_result(result, entry):
    if not isinstance(result, dict):
        raise Phase3PilotRunnerError("pilot executor returned no result")
    value = copy.deepcopy(result)
    if value.get("entry_id") != entry["entry_id"]:
        raise Phase3PilotRunnerError("pilot result entry binding is invalid")
    if value.get("terminal_status") not in _RESULT_STATUSES:
        raise Phase3PilotRunnerError("pilot result status is invalid")
    usage = value.get("usage")
    required_usage = {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "coverage_percent",
    }
    if not isinstance(usage, dict) or set(usage) != required_usage:
        raise Phase3PilotRunnerError("pilot result usage is invalid")
    if any(not isinstance(usage[field], int) or usage[field] < 0 for field in required_usage):
        raise Phase3PilotRunnerError("pilot result usage values are invalid")
    if usage["coverage_percent"] > 100 or usage["cached_input_tokens"] > usage[
        "input_tokens"
    ]:
        raise Phase3PilotRunnerError("pilot result usage relation is invalid")
    wall = value.get("wall_time_seconds")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool) or wall < 0:
        raise Phase3PilotRunnerError("pilot result wall time is invalid")
    score = value.get("official_score")
    if (
        not isinstance(score, dict)
        or score.get("status") not in {"completed", "failed"}
        or not isinstance(score.get("resolved"), bool)
    ):
        raise Phase3PilotRunnerError("pilot official score is invalid")
    if value.get("failure_class") is not None and value["failure_class"] not in (
        _RETRYABLE_FAILURES | {"resource_limit_exhausted", "evaluator_failure"}
    ):
        raise Phase3PilotRunnerError("pilot failure classification is invalid")
    _reject_forbidden_keys(value)
    return value


def _validate_official_score(score):
    if not isinstance(score, dict):
        raise Phase3PilotRunnerError("official evaluator returned no score")
    value = copy.deepcopy(score)
    if set(value) != {
        "schema_version",
        "status",
        "resolved",
        "patch_applied",
        "f2p_success",
        "f2p_total",
        "p2p_success",
        "p2p_total",
        "swe_style_partial_score",
        "image_manifest_digest",
        "evaluator_sha256",
        "report_sha256",
        "wall_time_seconds",
    }:
        raise Phase3PilotRunnerError("official score fields are invalid")
    if value["schema_version"] != "phase3_official_score.v1":
        raise Phase3PilotRunnerError("official score version is invalid")
    if value["status"] not in {"completed", "failed"} or not isinstance(
        value["resolved"], bool
    ):
        raise Phase3PilotRunnerError("official score outcome is invalid")
    if not isinstance(value["patch_applied"], bool):
        raise Phase3PilotRunnerError("official score patch status is invalid")
    for success, total in (("f2p_success", "f2p_total"), ("p2p_success", "p2p_total")):
        if (
            not isinstance(value[success], int)
            or isinstance(value[success], bool)
            or not isinstance(value[total], int)
            or isinstance(value[total], bool)
            or value[success] < 0
            or value[total] < value[success]
        ):
            raise Phase3PilotRunnerError("official score test counts are invalid")
    partial = value["swe_style_partial_score"]
    if (
        not isinstance(partial, (int, float))
        or isinstance(partial, bool)
        or partial < 0
        or partial > 1
    ):
        raise Phase3PilotRunnerError("official partial score is invalid")
    if (
        not isinstance(value["image_manifest_digest"], str)
        or not value["image_manifest_digest"].startswith("sha256:")
        or len(value["image_manifest_digest"]) != 71
    ):
        raise Phase3PilotRunnerError("official score image digest is invalid")
    for field in ("evaluator_sha256", "report_sha256"):
        digest = value[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise Phase3PilotRunnerError(f"official score {field} is invalid")
    wall = value["wall_time_seconds"]
    if not isinstance(wall, (int, float)) or isinstance(wall, bool) or wall < 0:
        raise Phase3PilotRunnerError("official score wall time is invalid")
    _reject_forbidden_keys(value)
    return value


def _reject_forbidden_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_RESULT_KEYS:
                raise Phase3PilotRunnerError(
                    "gold content key entered retained pilot result"
                )
            _reject_forbidden_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_keys(nested)


def _variance_percent(values):
    low, high = sorted(float(value) for value in values)
    if high == low:
        return 0.0
    if low == 0:
        return float("inf")
    return ((high - low) / low) * 100.0


def _verify_repository_mirror(source, repository):
    try:
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", f"{repository['commit']}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(source), "rev-parse", f"{repository['commit']}^{{tree}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        object_format = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-object-format"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Phase3PilotRunnerError("repository mirror is unavailable") from exc
    if (
        commit != repository["commit"]
        or tree != repository["tree"]
        or object_format != repository["git_object_format"]
    ):
        raise Phase3PilotRunnerError("repository mirror identity changed")


def _absolute_argv(command):
    if not isinstance(command, list) or not command or any(
        not isinstance(argument, str) or not argument for argument in command
    ):
        raise Phase3PilotRunnerError("acceptance command is invalid")
    executable = shutil.which(command[0])
    if executable is None:
        raise Phase3PilotRunnerError(
            f"acceptance executable is unavailable: {command[0]}"
        )
    return [str(Path(executable).resolve()), *command[1:]]


def _taskpack_acceptance_argv(command):
    """Express common test frontends through the taskpack-safe Python entrypoint."""

    if (
        isinstance(command, list)
        and command
        and isinstance(command[0], str)
        and Path(command[0]).name == "pytest"
    ):
        return ["python3", "-m", "pytest", *command[1:]]
    return list(command)


def _compact_id(value):
    compact = "".join(
        character.lower() if character.isalnum() else "-"
        for character in value
    ).strip("-")
    while "--" in compact:
        compact = compact.replace("--", "-")
    if len(compact) <= 80:
        return compact
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return compact[:67].rstrip("-") + "-" + digest


def _read_json(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Phase3PilotRunnerError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Phase3PilotRunnerError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise Phase3PilotRunnerError(f"{label} is not an object")
    return value


def _write_json_file(path, value):
    Path(path).write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="ascii",
    )


def _safe_id(value, label):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in value)
    ):
        raise Phase3PilotRunnerError(f"{label} is invalid")
    return value


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

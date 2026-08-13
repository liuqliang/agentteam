import hashlib
import json
import os
import stat
import subprocess
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .m0_runtime import (
    SystemClock,
    _append_jsonl,
    _create_git_worktree,
    _event,
    _find_idle_agent,
    _message_context_event_fields,
    _read_json,
    _repo_context_fields,
    _role_context_fields,
    _role_prompt_fields,
    _runtime_adapter_metadata,
    _scoped_id,
    apply_patch_to_integration_baseline_worktree,
    audit_worktree_diff,
    classify_attempt_outcome,
    commit_integration_baseline_worktree,
    create_independent_attempt_workspace,
    evaluate_integration_commit,
    ensure_integration_baseline_worktree,
    rebuild_sqlite_state_index,
    reset_integration_baseline_worktree,
    run_integration_verification_additions,
    run_integration_verification,
    skip_integration_baseline_commit,
    snapshot_runtime_artifacts,
    write_patch_artifact,
)
from .model_routing import default_model_routing_policy, select_model_route
from .retry_decision import decide_retry
from .integration_queue import integration_queue_path, upsert_integration_queue_item
from .decision_artifact_lifecycle import (
    apply_terminal_retention,
    compact_event,
    compact_scheduler_state,
    load_operator_report,
    publish_operator_report,
    publish_attempt_evidence,
    retire_patch,
    retire_transport,
)
from .git_code_state import (
    publish_attempt_code_state,
    publish_integration_code_state,
    restore_attempt_workspace,
)
from .decision_runtime import (
    inherited_decision_id,
    load_run_decision_binding,
    record_integration_acceptance,
    require_active_inherited_decision,
)
from .experiment_controller import (
    ExperimentControllerIntegrityError,
    load_experiment_controller,
    validate_experiment_controller_reference,
)
from .experiment_contract import canonical_json_sha256
from .experiment_sandbox import (
    build_provider_sandbox_descriptor,
    experiment_lifecycle_authority_root,
    load_experiment_mode_authority,
    publish_experiment_launch_registration,
    publish_provider_sandbox_reference,
)
from .notifications import DEFAULT_NOTIFICATION_EVENT_TYPES
from .operator_control import read_run_stop_request
from .planner_context import build_planner_context
from .runtime_artifacts import (
    bootstrap_completed_runtime_artifacts,
    materialize_runtime_input_artifacts,
    persist_runtime_artifacts,
    runtime_input_artifact_producers,
    validate_runtime_input_artifacts,
)
from .model_invocation import (
    InvocationLifecycle,
    ModelInvocationIntegrityError,
    append_canonical_events,
    assess_execution_group_fence,
    hydrate_provider_predecessor_context,
    import_author_lifecycle_bootstrap,
    import_model_invocation_lifecycle,
    import_registered_controller_lifecycles,
)
from .task_proposal import normalize_evidence_summary, normalize_task_proposal
from .token_usage import aggregate_token_usage, token_usage_from_result


_STOP_SCHEDULER_STATUSES = {"stopped", "stop_requested"}
_RUN_LOOP_TERMINAL_STATUSES = _STOP_SCHEDULER_STATUSES | {
    "budget_stopped",
    "interrupted",
}
WORKER_OUTBOX_PUBLICATION_GRACE_SECONDS = 2.0
WORKER_USAGE_STAGE_BY_ROLE = {
    "task_planner": "planner_or_task_slicer",
    "planner": "planner_or_task_slicer",
    "task_slicer": "planner_or_task_slicer",
    "repo_map_agent": "repo_map",
    "repo_map": "repo_map",
    "implementation_worker": "implementation_worker",
    "reviewer": "review_or_repair",
    "code_reviewer": "review_or_repair",
    "repair_worker": "review_or_repair",
    "review_or_repair": "review_or_repair",
    "follow_up_author": "follow_up_author",
    "semantic_architecture_agent": "semantic_architecture",
    "semantic_architecture": "semantic_architecture",
    "runtime_diagnostic": "runtime_diagnostic",
    "development_smoke": "development_smoke",
    "acceptance_live_smoke": "acceptance_live_smoke",
}
SUPPORTED_WORKER_INVOCATION_INVENTORY = (
    {
        "route": "decompose_backlog",
        "role": "*",
        "usage_stage": "planner_or_task_slicer",
    },
) + tuple(
    {"route": "worker", "role": role, "usage_stage": usage_stage}
    for role, usage_stage in sorted(WORKER_USAGE_STAGE_BY_ROLE.items())
)


class TwoPhaseFileScheduler:
    def __init__(
        self,
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=None,
        project_root=None,
        runtime_adapter=None,
        max_inflight=2,
        max_attempts=1,
        lease_timeout_seconds=900,
        integrate_accepted_patch=False,
        integration_verification_command=None,
        initial_integration_base_ref=None,
        commit_verified_integration=False,
        state_path=None,
        auto_decompose=False,
        decomposition_milestone_id="M21",
        decomposition_planner_role="task_planner",
        decomposition_default_worker_role="repo_map_agent",
        decomposition_allowed_read_scopes=None,
        decomposition_allowed_write_scopes=None,
        decomposition_context_artifact_paths=None,
        decomposition_context_excerpt_chars=1200,
        decomposition_max_waves=1,
        unavailable_agent_ids=None,
        notification_sink=None,
        invocation_fence_assessor=None,
        invocation_service_stopper=None,
        experiment_controller_reference=None,
        experiment_controller_required=False,
        resume_interrupted_experiment=False,
        experiment_controller_monotonic=None,
        independent_attempt_workspaces=False,
    ):
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if lease_timeout_seconds < 0:
            raise ValueError("lease_timeout_seconds must be at least 0")
        if decomposition_max_waves < 1:
            raise ValueError("decomposition_max_waves must be at least 1")
        if not isinstance(experiment_controller_required, bool):
            raise ValueError("experiment_controller_required must be a boolean")
        if not isinstance(resume_interrupted_experiment, bool):
            raise ValueError("resume_interrupted_experiment must be a boolean")
        if not isinstance(independent_attempt_workspaces, bool):
            raise ValueError(
                "independent_attempt_workspaces must be a boolean"
            )
        self.agent_pool_path = Path(agent_pool_path)
        self.backlog_path = Path(backlog_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.experiment_runtime_context = (
            _load_experiment_runtime_context(self.output_dir)
        )
        if self.experiment_runtime_context is not None:
            context_reference = self.experiment_runtime_context[
                "controller_reference"
            ]
            if (
                experiment_controller_reference is not None
                and experiment_controller_reference
                != context_reference
            ):
                raise ValueError(
                    "experiment runtime controller reference differs"
                )
            experiment_controller_reference = context_reference
            experiment_controller_required = True
            independent_attempt_workspaces = True
            if max_inflight != 1:
                raise ValueError(
                    "experiment runtime requires max_inflight=1"
                )
        self.clock = clock or SystemClock()
        self.project_root = Path(project_root) if project_root else None
        self.runtime_adapter = runtime_adapter
        self.max_inflight = max_inflight
        self.max_attempts = max_attempts
        self.lease_timeout_seconds = lease_timeout_seconds
        self.integrate_accepted_patch = integrate_accepted_patch
        self.integration_verification_command = integration_verification_command
        self.initial_integration_base_ref = initial_integration_base_ref
        self.commit_verified_integration = commit_verified_integration
        self.auto_decompose = auto_decompose
        self.decomposition_milestone_id = decomposition_milestone_id
        self.decomposition_planner_role = decomposition_planner_role
        self.decomposition_default_worker_role = decomposition_default_worker_role
        self.decomposition_allowed_read_scopes = list(
            decomposition_allowed_read_scopes or ["."]
        )
        self.decomposition_allowed_write_scopes = list(
            decomposition_allowed_write_scopes or ["generated/"]
        )
        self.decomposition_context_artifact_paths = list(
            decomposition_context_artifact_paths or []
        )
        self.decomposition_context_excerpt_chars = decomposition_context_excerpt_chars
        self.decomposition_max_waves = decomposition_max_waves
        self.unavailable_agent_ids = set(unavailable_agent_ids or [])
        self.notification_sink = notification_sink
        self.invocation_fence_assessor = invocation_fence_assessor
        self.invocation_service_stopper = invocation_service_stopper
        self.experiment_controller_reference = (
            validate_experiment_controller_reference(
                experiment_controller_reference
            )
            if experiment_controller_reference is not None
            else None
        )
        self.experiment_controller_required = bool(
            experiment_controller_required
            or self.experiment_controller_reference is not None
        )
        self.resume_interrupted_experiment = resume_interrupted_experiment
        self.experiment_controller_monotonic = experiment_controller_monotonic
        self.independent_attempt_workspaces = (
            independent_attempt_workspaces
        )
        self.experiment_controller = None
        self.state_path = Path(
            state_path or self.output_dir / "state" / "two_phase_scheduler_state.json"
        )
        self.state_db_path = self.output_dir / "state" / "scheduler_state.sqlite"
        self.events_path = self.output_dir / "events.jsonl"
        self.run_id = "RUN-TWO-PHASE-SCHEDULER"
        self.decision_binding = load_run_decision_binding(self.output_dir)
        self.state = self._load_or_create_state()
        bootstrap_completed_runtime_artifacts(
            self.output_dir,
            self.project_root,
            self.state["backlog"],
            source_ref=self.initial_integration_base_ref or "HEAD",
        )
        self._bind_experiment_runtime_context()
        self._bind_experiment_controller()

    def dispatch_ready(self):
        if self.stop_if_requested():
            return self._stopped_dispatch_result()
        import_author_lifecycle_bootstrap(self.output_dir)
        import_registered_controller_lifecycles(self.output_dir)
        controller_gate = self._prepare_experiment_dispatch()
        if controller_gate is not None:
            return controller_gate
        self._ensure_decomposition_task()
        effective_max_inflight = (
            1 if self.experiment_controller is not None else self.max_inflight
        )
        capacity = effective_max_inflight - len(
            self.state["inflight_attempts"]
        )
        if capacity <= 0:
            self.state["scheduler_status"] = "waiting"
            self._write_state()
            return {
                "dispatch_status": "at_capacity",
                "dispatched_task_ids": [],
                "dispatch_count": 0,
                "inflight_count": len(self.state["inflight_attempts"]),
            }

        agent_pool = _read_json(self.agent_pool_path)
        self._mark_inflight_agents_busy(agent_pool)
        self._mark_unavailable_agents(agent_pool)
        dispatched = []
        denied_observation = None
        for task in self._ready_tasks():
            if len(dispatched) >= capacity:
                break
            self._validate_trusted_task_controller_authority(task)
            denied_observation = self._observe_experiment_boundary(
                "pre_provider_launch"
            )
            if (
                denied_observation is not None
                and not denied_observation["allow_provider_launch"]
            ):
                break
            try:
                dispatch = self._dispatch_task(agent_pool, task)
            except ValueError as exc:
                if str(exc).startswith("no idle agent found for role "):
                    continue
                raise
            dispatched.append(dispatch)

        if (
            denied_observation is not None
            and not denied_observation["allow_provider_launch"]
            and not self.state["inflight_attempts"]
        ):
            denied_observation = self._observe_experiment_boundary(
                "post_integration"
            )
        controller_status = self.state.get("experiment_controller_status")
        self.state["scheduler_status"] = (
            controller_status
            if controller_status
            in {"budget_draining", "budget_stopped", "interrupted"}
            else "running"
            if dispatched
            else self._status_without_dispatch()
        )
        self._write_state()
        if dispatched:
            rebuild_sqlite_state_index(self.state_db_path, self.events_path)
        return {
            "dispatch_status": (
                "dispatched"
                if dispatched
                else denied_observation["controller_status"]
                if denied_observation is not None
                and not denied_observation["allow_provider_launch"]
                else "idle"
            ),
            "dispatched_task_ids": [item["task_id"] for item in dispatched],
            "dispatch_count": len(dispatched),
            "inflight_count": len(self.state["inflight_attempts"]),
        }

    def collect_ready_results(self):
        if self.stop_if_requested():
            return self._stopped_collect_result()
        experiment_preparation = self._prepare_experiment_dispatch()
        if (
            experiment_preparation is not None
            and experiment_preparation["dispatch_status"]
            == "integration_recovery_required"
        ):
            return {
                **self._empty_collect_result(),
                "collect_status": "integration_recovery_required",
            }
        import_registered_controller_lifecycles(self.output_dir)
        collected = []
        remaining = []
        for inflight in self.state["inflight_attempts"]:
            self._import_worker_lifecycles(inflight)
            result = _runtime_result_from_outbox(
                inflight["outbox_path"],
                inflight["message_id"],
            )
            if result is not None and self.experiment_controller is not None:
                reconciliation = reconcile_orphaned_invocation(
                    self._inflight_invocation_authority_root(inflight),
                    inflight,
                    fence_assessor=self.invocation_fence_assessor,
                    service_stopper=self.invocation_service_stopper,
                )
                inflight["invocation_reconciliation"] = reconciliation
                reconciliation_status = reconciliation[
                    "reconciliation_status"
                ]
                if (
                    reconciliation_status == "no_invocation"
                    and self._lease_expired(inflight)
                ):
                    suspicious_result = result
                    suspicious_changed_files = suspicious_result.get(
                        "changed_files"
                    )
                    suspicious_output = suspicious_result.get("output")
                    result = self._timeout_runtime_result(inflight)
                    result["output"].update(
                        {
                            "error": (
                                "lease_expired_without_provider_start"
                            ),
                            "invocation_reconciliation": reconciliation,
                            "suspicious_outbox_result": {
                                "result_status": suspicious_result.get(
                                    "result_status"
                                ),
                                "changed_files_type": type(
                                    suspicious_changed_files
                                ).__name__,
                                "changed_files": [
                                    value[:500]
                                    for value in (
                                        suspicious_changed_files
                                        if isinstance(
                                            suspicious_changed_files,
                                            list,
                                        )
                                        else []
                                    )[:100]
                                    if isinstance(value, str)
                                ],
                                "output_type": type(
                                    suspicious_output
                                ).__name__,
                                "output_keys": sorted(
                                    str(key)[:200]
                                    for key in (
                                        suspicious_output
                                        if isinstance(
                                            suspicious_output,
                                            dict,
                                        )
                                        else {}
                                    )
                                )[:100],
                            },
                        }
                    )
                elif reconciliation_status not in {
                    "terminal_available",
                    "recovered",
                }:
                    remaining.append(inflight)
                    continue
            if result is None:
                lease_expired = self._lease_expired(inflight)
                reconciliation = None
                if self.experiment_controller is not None or lease_expired:
                    reconciliation = reconcile_orphaned_invocation(
                        self._inflight_invocation_authority_root(
                            inflight
                        ),
                        inflight,
                        fence_assessor=self.invocation_fence_assessor,
                        service_stopper=self.invocation_service_stopper,
                    )
                    inflight["invocation_reconciliation"] = reconciliation
                    if reconciliation["reconciliation_status"] in {
                        "live",
                        "open_ambiguous",
                        "service_stop_required",
                    }:
                        remaining.append(inflight)
                        continue
                    if (
                        reconciliation["reconciliation_status"]
                        in {"terminal_available", "recovered"}
                        and not lease_expired
                        and not self.resume_interrupted_experiment
                    ):
                        observed = inflight.get(
                            "terminal_without_outbox_observed"
                        )
                        terminal_path = reconciliation.get("terminal_path")
                        now_monotonic = (
                            self.experiment_controller_monotonic()
                            if self.experiment_controller_monotonic
                            is not None
                            else time.monotonic()
                        )
                        if (
                            not isinstance(observed, dict)
                            or observed.get("terminal_path")
                            != terminal_path
                            or not isinstance(
                                observed.get("observed_at_monotonic"),
                                (int, float),
                            )
                            or observed["observed_at_monotonic"]
                            > now_monotonic
                        ):
                            observed = {
                                "terminal_path": terminal_path,
                                "reconciliation_status": reconciliation.get(
                                    "reconciliation_status"
                                ),
                                "observed_at_monotonic": now_monotonic,
                            }
                            inflight[
                                "terminal_without_outbox_observed"
                            ] = observed
                        if (
                            now_monotonic
                            - observed["observed_at_monotonic"]
                            < WORKER_OUTBOX_PUBLICATION_GRACE_SECONDS
                        ):
                            # The provider terminal precedes the richer worker
                            # outbox. Preserve a bounded publication window so
                            # terminal-only recovery cannot discard semantic
                            # evidence from a healthy worker.
                            remaining.append(inflight)
                            continue
                    result = _runtime_result_from_reconciliation(
                        inflight,
                        reconciliation,
                    )
                if result is None and not lease_expired:
                    remaining.append(inflight)
                    continue
                if result is None:
                    result = self._timeout_runtime_result(inflight)
                    result["output"]["invocation_reconciliation"] = (
                        reconciliation
                    )
            collected.append(self._collect_result(inflight, result))

        self.state["inflight_attempts"] = remaining
        controller_observation = None
        if collected or not remaining:
            controller_observation = self._observe_experiment_boundary(
                "post_integration"
            )
        self.state["scheduler_status"] = self._status_without_dispatch()
        self._write_state()
        if collected:
            rebuild_sqlite_state_index(self.state_db_path, self.events_path)
        response = {
            "collect_status": "collected" if collected else "idle",
            "collected_task_ids": [item["task_id"] for item in collected],
            "collected_count": len(collected),
            "inflight_count": len(self.state["inflight_attempts"]),
            "results": collected,
        }
        if controller_observation is not None:
            response.update(
                {
                    "experiment_controller_status": controller_observation[
                        "controller_status"
                    ],
                    "experiment_budget_state": deepcopy(
                        self.state["experiment_budget_state"]
                    ),
                }
            )
        return response

    def tick(self):
        stopped = self.stop_if_requested()
        if stopped:
            return stopped
        collect = self.collect_ready_results()
        dispatch = self.dispatch_ready()
        if self.state.get("scheduler_status") in _RUN_LOOP_TERMINAL_STATUSES:
            tick_status = self.state["scheduler_status"]
        elif collect["collected_count"] or dispatch["dispatch_count"]:
            tick_status = "running"
        elif self.state["inflight_attempts"]:
            tick_status = "waiting"
        else:
            tick_status = "idle"
        return {
            "tick_status": tick_status,
            "collect": collect,
            "dispatch": dispatch,
            "inflight_count": len(self.state["inflight_attempts"]),
            "processed_task_ids": self.summary()["processed_task_ids"],
        }

    def set_unavailable_agent_ids(self, agent_ids):
        self.unavailable_agent_ids = set(agent_ids or [])

    def run_until_idle(self, max_ticks=100, poll_interval_seconds=0.02):
        if max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        stopped = self.stop_if_requested()
        if stopped:
            self._emit_run_event_once(
                "run_stopped",
                self._run_event_payload("stopped", {"tick_count": 0}),
            )
            return {
                **self.summary(),
                "scheduler_status": self.state["scheduler_status"],
                "tick_count": 0,
                "last_tick": stopped,
            }
        self._emit_run_event_once(
            "run_started",
            self._run_event_payload("running", {"max_ticks": max_ticks}),
        )
        tick_count = 0
        last_tick = None
        for _ in range(max_ticks):
            tick_count += 1
            last_tick = self.tick()
            if last_tick["tick_status"] in _RUN_LOOP_TERMINAL_STATUSES:
                self._emit_run_event_once(
                    "run_stopped",
                    self._run_event_payload(
                        last_tick["tick_status"],
                        {"tick_count": tick_count},
                    ),
                )
                return {
                    **self.summary(),
                    "scheduler_status": self.state["scheduler_status"],
                    "tick_count": tick_count,
                    "last_tick": last_tick,
                }
            if last_tick["tick_status"] == "idle":
                completion = self.complete_verified_backlog(tick_count)
                return {
                    **self.summary(),
                    "scheduler_status": completion["scheduler_status"],
                    "tick_count": tick_count,
                    "last_tick": last_tick,
                    "milestone_status": completion["milestone_status"],
                    "next_action": completion.get("next_action"),
                }
            if last_tick["tick_status"] == "waiting":
                time.sleep(poll_interval_seconds)
        self.state["scheduler_status"] = "max_ticks_reached"
        self._write_state()
        self._emit_run_event_once(
            "run_timed_out",
            self._run_event_payload("max_ticks_reached", {"tick_count": tick_count}),
        )
        return {
            **self.summary(),
            "scheduler_status": "max_ticks_reached",
            "tick_count": tick_count,
            "last_tick": last_tick,
        }

    def summary(self):
        active_inflight_count = self._active_inflight_count()
        inactive_inflight_count = self._inactive_inflight_count()
        processed_task_ids = [
            step["task_id"]
            for step in self.state["steps"]
            if step["step_status"] == "processed"
        ]
        return {
            "scheduler_status": self.state["scheduler_status"],
            "processed_task_ids": processed_task_ids,
            "step_count": len(self.state["steps"]),
            "inflight_count": active_inflight_count,
            "inactive_inflight_count": inactive_inflight_count,
            "max_attempts": self.state["max_attempts"],
            "lease_timeout_seconds": self.state["lease_timeout_seconds"],
            "steps": self.state["steps"],
            "events_path": str(self.events_path),
            "state_path": str(self.state_path),
            "state_db_path": str(self.state_db_path),
            "experiment_controller_status": self.state.get(
                "experiment_controller_status"
            ),
            "experiment_budget_state": deepcopy(
                self.state.get("experiment_budget_state")
            ),
            "integration_active": self.state.get("integration_active", False),
        }

    def complete_verified_backlog(self, tick_count):
        """Project verified idle without claiming gated milestone completion."""
        if not self._requires_post_backlog_gates():
            self._emit_run_event_once(
                "run_completed",
                self._run_event_payload("completed", {"tick_count": tick_count}),
            )
            return {
                "scheduler_status": "idle",
                "milestone_status": "completed",
                "next_action": None,
            }

        self.state["scheduler_status"] = "awaiting_post_backlog_gates"
        self._write_state()
        next_action = (
            f"agentteam gate seal-baseline --taskpack {self.output_dir.name}"
        )
        self._emit_run_event_once(
            "backlog_completed",
            self._run_event_payload(
                "awaiting_post_backlog_gates",
                {
                    "tick_count": tick_count,
                    "backlog_status": "completed",
                    "milestone_status": "awaiting_post_backlog_gates",
                    "next_action": next_action,
                },
            ),
        )
        return {
            "scheduler_status": "awaiting_post_backlog_gates",
            "milestone_status": "awaiting_post_backlog_gates",
            "next_action": next_action,
        }

    def _requires_post_backlog_gates(self):
        return (
            self.output_dir
            / "state"
            / "post_backlog_gates"
            / "gate_state.v1.json"
        ).is_file()

    def stop_if_requested(self):
        if not self._apply_run_stop_request():
            return None
        return self._stopped_tick_result()

    def stop_inflight_invocations(self):
        """Stop exact durable invocation services for every inflight attempt."""

        results = []
        for inflight in self.state.get("inflight_attempts", []):
            authority_root = self._inflight_invocation_authority_root(inflight)
            matches = _matching_invocation_starts(authority_root, inflight)
            if not matches:
                results.append(
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "stop_status": "no_invocation",
                    }
                )
                continue
            if len(matches) != 1:
                results.append(
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "stop_status": "ambiguous",
                        "invocation_ids": [
                            record.get("invocation_id") for _, record in matches
                        ],
                    }
                )
                continue
            started_path, start = matches[0]
            if started_path.with_name("terminal.json").is_file():
                stop_status = "already_terminal"
            else:
                stopper = self.invocation_service_stopper or _stop_exact_transient_service
                stop_status = "stopped" if stopper(deepcopy(start)) else "stop_failed"
            results.append(
                {
                    "task_id": inflight["task_id"],
                    "attempt_id": inflight["attempt_id"],
                    "invocation_id": start.get("invocation_id"),
                    "stop_status": stop_status,
                }
            )
        return {
            "requested_count": len(self.state.get("inflight_attempts", [])),
            "stopped_count": sum(
                item["stop_status"] in {"stopped", "already_terminal"}
                for item in results
            ),
            "results": results,
        }

    def _dispatch_task(self, agent_pool, task):
        decision_id = require_active_inherited_decision(
            self.decision_binding,
            task["task_id"],
        )
        if decision_id is not None:
            task["decision_id"] = decision_id
        step_id = self._next_step_id(task["task_id"])
        step_dir = self.output_dir / "steps" / step_id
        step_dir.mkdir(parents=True, exist_ok=True)
        step_backlog_path = step_dir / "backlog.json"
        step_backlog_path.write_text(
            json.dumps(
                {
                    "backlog_id": self.state["backlog"].get(
                        "backlog_id",
                        "BL-TWO-PHASE-STEP",
                    ),
                    "items": [deepcopy(task)],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        unavailable_agent_ids = self._unavailable_agent_ids_for_role(
            agent_pool,
            task["required_role"],
        )
        agent = _find_idle_agent(agent_pool, task["required_role"])
        attempt_number = self._next_attempt_number(task["task_id"])
        attempt_id = f"{task['task_id']}-ATTEMPT-{attempt_number:03d}"
        lease_id = f"{task['task_id']}-LEASE-{attempt_number:03d}"
        message_id = _scoped_id("MSG", attempt_number, task["task_id"], width=4)
        worktree_id = (
            f"WT-{attempt_id}"
            if (
                task.get("write_scope")
                or self.experiment_runtime_context is not None
            )
            else None
        )
        runtime_session_id = f"SESSION-{attempt_id}"
        worktree_path = None
        branch = None
        integration_baseline = None
        correlation_id = f"{task['task_id']}:{attempt_id}"
        created_at = self.clock.now()
        retry_handoff = self._retry_handoff_for_task(task["task_id"])
        prior_retry_decision = (
            retry_handoff.get("retry_decision")
            if isinstance(retry_handoff, dict)
            else None
        )
        lease_expires_at = _timestamp_after(
            created_at,
            self.state["lease_timeout_seconds"],
        )
        invocation_context = _worker_invocation_context(
            agent_pool,
            self.state["backlog"],
            task,
            agent,
            run_id=self.run_id,
            runtime_execution_session_id=runtime_session_id,
            lease_id=lease_id,
            output_dir=self.output_dir,
            project_root=self.project_root,
            experiment_controller_reference=(
                self.experiment_controller_reference
            ),
            experiment_controller_required=(
                self.experiment_controller_required
            ),
            experiment_authority_root=(
                self.experiment_controller_reference["controller_root"]
                if self.experiment_controller_reference is not None
                else None
            ),
        )
        model_route = select_model_route(
            agent_pool.get("model_routing_policy"),
            role=agent.get("role") or task.get("required_role"),
            risk_target=task.get("risk_target") or "L1",
            attempt_number=attempt_number,
            retry_decision=prior_retry_decision,
        )
        if self.experiment_runtime_context is not None:
            experiment_model_policy = self.experiment_runtime_context[
                "model_policy"
            ]
            model_route = select_model_route(
                default_model_routing_policy(
                    fixed_profile={
                        "model": experiment_model_policy["model"],
                        "reasoning_profile": experiment_model_policy[
                            "reasoning_profile"
                        ],
                    }
                ),
                role=agent.get("role") or task.get("required_role"),
                risk_target=task.get("risk_target") or "L1",
                attempt_number=attempt_number,
                retry_decision=prior_retry_decision,
            )
        if model_route is not None:
            invocation_context.update(
                {
                    "model": model_route["model"],
                    "reasoning_profile": model_route["reasoning_profile"],
                    "model_routing": model_route,
                }
            )
        invocation_context = hydrate_provider_predecessor_context(
            invocation_context,
            self.events_path,
        )
        runtime_input_artifact_producers = self._runtime_input_artifact_producers(
            task
        )
        validate_runtime_input_artifacts(
            self.output_dir,
            runtime_input_artifact_producers,
        )
        agent["status"] = "busy"
        agent["lease"] = {
            "lease_id": lease_id,
            "task_id": task["task_id"],
            "expires_at": lease_expires_at,
        }

        if self.project_root and worktree_id:
            integration_baseline = self._ensure_integration_baseline() if self.integrate_accepted_patch else None
            if self.independent_attempt_workspaces:
                worktree_path, branch = (
                    create_independent_attempt_workspace(
                        self.project_root,
                        step_dir,
                        attempt_id,
                        worktree_id,
                        base_repository=(
                            integration_baseline[
                                "integration_baseline_worktree_path"
                            ]
                            if integration_baseline
                            else self.project_root
                        ),
                        base_ref="HEAD",
                    )
                )
            else:
                worktree_path, branch = _create_git_worktree(
                    self.project_root,
                    step_dir,
                    attempt_id,
                    worktree_id,
                    base_ref=(
                        integration_baseline[
                            "integration_baseline_branch"
                        ]
                        if integration_baseline
                        else None
                    ),
                )
        materialized_input_artifacts = self._materialize_input_artifacts(
            runtime_input_artifact_producers,
            worktree_path,
        )
        role_context_fields = _role_context_fields(
            agent_pool,
            agent,
            step_dir,
            attempt_id,
            project_root=self.project_root,
        )
        repo_context_fields = _repo_context_fields(
            self.project_root,
            self.output_dir,
            task,
            agent,
            attempt_id,
        )
        provider_io_fields = {}
        if self.experiment_runtime_context is not None:
            provider_io_fields = self._stage_experiment_provider_io(
                worktree_path,
                attempt_id,
                {
                    **role_context_fields,
                    **repo_context_fields,
                },
            )
            invocation_context = self._register_experiment_attempt_launch(
                invocation_context,
                worktree_path=worktree_path,
                attempt_id=attempt_id,
                taskpack_id=invocation_context["taskpack_id"],
            )
        runtime_artifact_baseline = snapshot_runtime_artifacts(
            worktree_path,
            task.get("expected_output_artifacts", []),
        )
        message = {
            "message_id": message_id,
            "from_agent": agent_pool["scheduler_agent_id"],
            "to_agent": agent["agent_id"],
            "message_type": "dispatch_task",
            "correlation_id": correlation_id,
            "created_at": created_at,
            "lease_expires_at": lease_expires_at,
            "payload": {
                "task_id": task["task_id"],
                **({"decision_id": decision_id} if decision_id else {}),
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "worktree_id": worktree_id,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "branch": branch,
                "task_kind": task.get("task_kind", "implementation"),
                "milestone_id": task.get("milestone_id"),
                "default_worker_role": task.get("default_worker_role"),
                "planner_context_path": task.get("planner_context_path"),
                "objective": task["objective"],
                "goal_alignment": task.get("goal_alignment"),
                "required_deliverables": task.get("required_deliverables", []),
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
                "input_artifacts": task.get("input_artifacts", []),
                "expected_output_artifacts": task.get("expected_output_artifacts", []),
                "materialized_input_artifacts": materialized_input_artifacts,
                **(
                    {"retry_handoff": retry_handoff}
                    if retry_handoff is not None
                    else {}
                ),
                **invocation_context,
                **_evidence_policy_fields(task),
                **_operator_guidance_fields(task),
                **_permission_grant_fields(task),
                **_role_prompt_fields(agent_pool, agent, task),
                **role_context_fields,
                **repo_context_fields,
                **provider_io_fields,
                **(
                    {
                        "model": model_route["model"],
                        "reasoning_profile": model_route["reasoning_profile"],
                        "model_routing": model_route,
                    }
                    if model_route is not None
                    else {}
                ),
            },
        }
        _write_dispatch_authority(step_dir, message)
        inbox_path = step_dir / agent["inbox_path"]
        _append_jsonl(inbox_path, [message])

        if self.runtime_adapter:
            metadata = {
                **_runtime_adapter_metadata(self.runtime_adapter),
                "runtime_profile_source": "explicit_runtime_adapter",
            }
        else:
            metadata = {
                "runtime_adapter": "FileMailboxExternalRuntimeAdapter",
                "runtime_model": (
                    model_route["model"] if model_route is not None else None
                ),
                "runtime_reasoning_profile": (
                    model_route["reasoning_profile"]
                    if model_route is not None
                    else None
                ),
                "runtime_sandbox": None,
                "runtime_timeout_seconds": None,
                "runtime_profile_source": (
                    "dispatch_model_routing"
                    if model_route is not None
                    else "external_mailbox_adapter"
                ),
            }
        self._append_events(
            step_id,
            [
                self._event(
                    "task_selected",
                    agent_pool["scheduler_agent_id"],
                    None,
                    f"select:{task['task_id']}:{attempt_id}",
                    correlation_id,
                    {"task_id": task["task_id"], "attempt_id": attempt_id},
                ),
                *(
                    [
                        self._event(
                            "task_reassigned",
                            agent_pool["scheduler_agent_id"],
                            agent["agent_id"],
                            f"reassign:{task['task_id']}:{attempt_id}",
                            correlation_id,
                            {
                                "task_id": task["task_id"],
                                "attempt_id": attempt_id,
                                "lease_id": lease_id,
                                "required_role": task["required_role"],
                                "unavailable_agent_ids": unavailable_agent_ids,
                                "selected_agent_id": agent["agent_id"],
                                "reassignment_reason": "agent_unavailable",
                            },
                        )
                    ]
                    if unavailable_agent_ids
                    else []
                ),
                self._event(
                    "lease_acquired",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"lease:{lease_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "lease_status": "active",
                        "lease_expires_at": lease_expires_at,
                    },
                ),
                *(
                    [
                        self._event(
                            "worktree_created",
                            agent_pool["scheduler_agent_id"],
                            agent["agent_id"],
                            f"worktree:{worktree_id}",
                            correlation_id,
                            {
                                "task_id": task["task_id"],
                                "attempt_id": attempt_id,
                                "worktree_id": worktree_id,
                                "worktree_path": str(worktree_path),
                                "branch": branch,
                                **_integration_baseline_event_fields(integration_baseline),
                                "write_scope": task["write_scope"],
                            },
                        )
                    ]
                    if worktree_id and worktree_path
                    else []
                ),
                self._event(
                    "message_dispatched",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"dispatch:{message_id}",
                    correlation_id,
                    {
                        "message_id": message_id,
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "materialized_input_artifacts": materialized_input_artifacts,
                        **_message_context_event_fields(message["payload"]),
                    },
                ),
                self._event(
                    "runtime_session_started",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"runtime-session-started:{runtime_session_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "runtime_session_id": runtime_session_id,
                        **metadata,
                        **(
                            {"model_routing": model_route}
                            if model_route is not None
                            else {}
                        ),
                        "worktree_id": worktree_id,
                        "worktree_path": str(worktree_path) if worktree_path else None,
                        **_integration_baseline_event_fields(integration_baseline),
                        "session_status": "started",
                    },
                ),
            ],
        )

        inflight = {
            "step_id": step_id,
            "step_dir": str(step_dir),
            "task_id": task["task_id"],
            **({"decision_id": decision_id} if decision_id else {}),
            "attempt_number": attempt_number,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "lease_expires_at": lease_expires_at,
            **({"created_at": created_at} if decision_id else {}),
            "message_id": message_id,
            "runtime_session_id": runtime_session_id,
            **({"model_routing": model_route} if model_route is not None else {}),
            "agent_id": agent["agent_id"],
            "outbox_path": str(step_dir / agent["outbox_path"]),
            "worktree_id": worktree_id,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "branch": branch,
            "model_invocation_authority_root": invocation_context[
                "model_invocation_authority_root"
            ],
            "runtime_artifact_baseline": runtime_artifact_baseline,
            **_integration_baseline_inflight_fields(integration_baseline),
            "correlation_id": correlation_id,
        }
        self.state["inflight_attempts"].append(inflight)
        return {"task_id": task["task_id"], "step_id": step_id}

    def _runtime_input_artifact_producers(self, task):
        return runtime_input_artifact_producers(
            self.state["backlog"],
            task,
        )

    def _materialize_input_artifacts(
        self,
        artifact_producers,
        worktree_path,
    ):
        if not worktree_path:
            return []
        return materialize_runtime_input_artifacts(
            self.output_dir,
            worktree_path,
            artifact_producers,
        )

    def _persist_runtime_artifacts(
        self,
        task,
        inflight,
        artifact_digests,
    ):
        if not artifact_digests:
            return []
        return persist_runtime_artifacts(
            self.output_dir,
            inflight["worktree_path"],
            artifact_digests,
            task_id=task["task_id"],
            attempt_id=inflight["attempt_id"],
        )

    def _collect_result(self, inflight, runtime_result):
        prior_result = self._completed_attempt_result(inflight)
        if prior_result is not None:
            return prior_result
        worktree_recovery = self._restore_inflight_worktree_if_missing(inflight)
        self._import_worker_lifecycles(inflight)
        task = self._task_by_id(inflight["task_id"])
        diff_audit = (
            audit_worktree_diff(
                inflight["worktree_path"],
                runtime_result["changed_files"],
                runtime_artifact_paths=task.get("expected_output_artifacts", []),
                runtime_artifact_baseline=inflight.get(
                    "runtime_artifact_baseline",
                    {},
                ),
                required_changed_files=task.get("expected_output_artifacts", []),
            )
            if inflight["worktree_path"]
            else None
        )
        patch_path = (
            write_patch_artifact(
                inflight["worktree_path"],
                self.output_dir / "attempts" / inflight["attempt_id"],
                diff_audit["actual_changed_files"],
            )
            if inflight["worktree_path"]
            and diff_audit
            and diff_audit["actual_changed_files"]
            else None
        )
        outcome = classify_attempt_outcome(runtime_result, task, diff_audit=diff_audit)
        runtime_artifacts = []
        permission_request = (
            _permission_request_payload(inflight, runtime_result)
            if runtime_result["result_status"] == "blocked"
            and not _runtime_result_has_manual_gate(runtime_result)
            else None
        )
        manual_gate = (
            _manual_gate_payload(inflight, runtime_result)
            if runtime_result["result_status"] == "blocked" and not permission_request
            else None
        )
        next_attempt_id = (
            f"{inflight['task_id']}-ATTEMPT-{inflight['attempt_number'] + 1:03d}"
        )
        result_actor = (
            "agent-scheduler"
            if runtime_result["result_status"] == "timed_out"
            else inflight["agent_id"]
        )
        result = {
            "task_id": inflight["task_id"],
            **(
                {"decision_id": inflight["decision_id"]}
                if inflight.get("decision_id")
                else {}
            ),
            "attempt_number": inflight["attempt_number"],
            "attempt_id": inflight["attempt_id"],
            "lease_id": inflight["lease_id"],
            "message_id": inflight["message_id"],
            "runtime_session_id": inflight["runtime_session_id"],
            "runtime_session_status": "stopped",
            "changed_files": list(runtime_result["changed_files"]),
            "runtime_output": runtime_result.get("output", {}),
            "token_usage": token_usage_from_result(runtime_result),
            "worktree_id": inflight["worktree_id"],
            "worktree_path": inflight["worktree_path"],
            "branch": inflight["branch"],
            "integration_base_ref": inflight.get("integration_base_ref"),
            "integration_base_sha": inflight.get("integration_base_sha"),
            "integration_baseline_branch": inflight.get("integration_baseline_branch"),
            "integration_baseline_worktree_path": inflight.get(
                "integration_baseline_worktree_path"
            ),
            "validation_status": outcome["validation_status"],
            "failure_category": outcome["failure_category"],
            "retryable": outcome["retryable"],
            "semantic_validation": outcome.get("semantic_validation"),
            "diff_audit": diff_audit,
            "patch_path": str(patch_path) if patch_path else None,
            "runtime_artifacts": runtime_artifacts,
            "integration_status": "not_requested",
            "integration_branch": None,
            "integration_worktree_path": None,
            "integration_verification_status": "not_requested",
            "integration_verification_exit_code": None,
            "integration_verification_stdout": "",
            "integration_verification_stderr": "",
            "integration_verification_additions_status": "not_requested",
            "integration_verification_additions": [],
            "integration_commit_status": "not_requested",
            "integration_commit_sha": None,
            "integration_commit_message": None,
            "integration_commit_reason": None,
            "integration_commit_stdout": "",
            "integration_commit_stderr": "",
            "integration_baseline_commit_status": "not_requested",
            "integration_baseline_commit_sha": None,
            "integration_baseline_commit_message": None,
            "integration_baseline_commit_reason": None,
            "integration_baseline_commit_stdout": "",
            "integration_baseline_commit_stderr": "",
            "integration_baseline_rollback_status": "not_requested",
            "integration_baseline_rollback_stdout": "",
            "integration_baseline_rollback_stderr": "",
            "integration_queue_status": "not_queued",
            "integration_queue_item_id": None,
            "integration_queue_path": str(integration_queue_path(self.output_dir)),
            "pre_integration_controller_observation": None,
            "post_integration_controller_observation": None,
        }
        result.update(_runtime_evidence_summary(task, runtime_result))
        code_state = None
        code_state_events = []
        if (
            self.decision_binding is not None
            and self.project_root is not None
            and inflight.get("worktree_path")
            and diff_audit is not None
        ):
            code_state = publish_attempt_code_state(
                self.decision_binding,
                project_root=self.project_root,
                worktree_path=inflight["worktree_path"],
                run_id=self.output_dir.name,
                task_id=inflight["task_id"],
                attempt_id=inflight["attempt_id"],
                changed_files=diff_audit["actual_changed_files"],
                created_at=inflight["created_at"],
                validation_status=outcome["validation_status"],
            )
            result.update(code_state)
            code_state_events.append(
                self._event(
                    "code_state_published",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"code-state:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        **code_state,
                    },
                )
            )
        integration_transaction_active = False
        pre_integration_observation = None
        if (
            outcome["validation_status"] == "accepted"
            and patch_path
            and not _integration_blocked_by_evidence(result, patch_path)
        ):
            pre_integration_observation = self._observe_experiment_boundary(
                "pre_integration"
            )
            result["pre_integration_controller_observation"] = (
                deepcopy(pre_integration_observation)
            )
        if (
            pre_integration_observation is not None
            and not pre_integration_observation["allow_integration"]
        ):
            integration_events = self._preserve_accepted_patch(
                inflight,
                result,
                patch_path,
                pre_integration_observation,
            )
        else:
            integration_transaction_active = bool(
                outcome["validation_status"] == "accepted"
                and patch_path
                and self.integrate_accepted_patch
                and self.project_root
                and not _integration_blocked_by_evidence(result, patch_path)
            )
            if integration_transaction_active:
                self.state["integration_active"] = True
                self.state["integration_attempt_id"] = inflight["attempt_id"]
                self._write_state()
            integration_events = self._integrate_accepted_result(
                inflight,
                result,
                patch_path,
                outcome,
            )
        decomposition_events = self._apply_decomposition_result(
            inflight,
            runtime_result,
            result,
            outcome,
        )
        transition = self._verified_backlog_transition(
            inflight,
            result,
            patch_path,
            outcome,
        )
        result.update(transition)
        retry_decision = None
        if result["task_status"] != "done":
            retry_decision = decide_retry(
                attempt_id=inflight["attempt_id"],
                failure_category=result["failure_category"],
                retryable=result["retryable"],
                runtime_result=runtime_result,
                attempt_result=result,
            )
        result["retry_decision"] = retry_decision
        retry_allowed = (
            retry_decision is not None
            and retry_decision["auto_retry"]
            and inflight["attempt_number"] < self.state["max_attempts"]
        )
        result["retry_allowed"] = retry_allowed
        if transition["task_status"] == "retryable":
            result["task_status"] = "ready" if retry_allowed else "blocked"
        projected_task_status = (
            "ready" if retry_allowed else result["task_status"]
        )
        if self.decision_binding is not None and code_state is not None:
            result.update(retire_patch(patch_path))
            patch_path = None
            if result.get("integration_queue_item_id"):
                result.update(upsert_integration_queue_item(self.output_dir, result))
        if result["task_status"] == "done" and diff_audit:
            runtime_artifacts = self._persist_runtime_artifacts(
                task,
                inflight,
                diff_audit["runtime_artifact_digests"],
            )
            result["runtime_artifacts"] = runtime_artifacts
        if self.decision_binding is not None and code_state is not None:
            evidence_artifact = publish_attempt_evidence(
                self.decision_binding,
                self.output_dir,
                result,
                created_at=inflight["created_at"],
            )
            result.update(evidence_artifact)
        runtime_events = [
                self._event(
                    "runtime_session_observed",
                    result_actor,
                    "agent-scheduler",
                    f"runtime-session-observed:{inflight['runtime_session_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "runtime_session_id": inflight["runtime_session_id"],
                        "result_status": runtime_result["result_status"],
                        "changed_file_count": len(runtime_result["changed_files"]),
                        "session_status": "observed",
                    },
                ),
                self._event(
                    "runtime_output_received",
                    result_actor,
                    "agent-scheduler",
                    f"runtime-result:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "result_status": runtime_result["result_status"],
                        "changed_files": runtime_result["changed_files"],
                        "output": runtime_result.get("output", {}),
                        "diff_audit": diff_audit,
                        "patch_path": str(patch_path) if patch_path else None,
                        "runtime_artifacts": runtime_artifacts,
                    },
                ),
                self._event(
                    "runtime_session_stopped",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"runtime-session-stopped:{inflight['runtime_session_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "runtime_session_id": inflight["runtime_session_id"],
                        "result_status": runtime_result["result_status"],
                        "session_status": "stopped",
                    },
                ),
                self._event(
                    "validation_accepted"
                    if outcome["validation_status"] == "accepted"
                    else "validation_rejected",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"validate:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "validation_status": outcome["validation_status"],
                        "failure_category": outcome["failure_category"],
                        "retryable": outcome["retryable"],
                        "lease_id": inflight["lease_id"],
                        "diff_audit": diff_audit,
                        "patch_path": str(patch_path) if patch_path else None,
                        "runtime_artifacts": runtime_artifacts,
                        "semantic_validation": outcome.get("semantic_validation"),
                        **_decomposition_validation_payload(result),
                    },
                ),
                *(
                    [
                        self._event(
                            "manual_gate_required",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"manual-gate:{manual_gate['question_id']}",
                            inflight["correlation_id"],
                            manual_gate,
                        )
                    ]
                    if manual_gate
                    else []
                ),
                *(
                    [
                        self._event(
                            "permission_request_required",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"permission-request:{permission_request['request_id']}",
                            inflight["correlation_id"],
                            permission_request,
                        )
                    ]
                    if permission_request
                    else []
                ),
                *code_state_events,
                *(
                    [
                        self._event(
                            "code_state_recovered",
                            "recovery-controller",
                            inflight["agent_id"],
                            f"code-state-recovered:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                **worktree_recovery,
                            },
                        )
                    ]
                    if worktree_recovery
                    else []
                ),
                *integration_events,
                *decomposition_events,
                *(
                    [
                        self._event(
                            "retry_decision_recorded",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"retry-decision:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                "retry_decision": retry_decision,
                            },
                        )
                    ]
                    if retry_decision is not None
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-done:{inflight['task_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "done",
                                "lease_id": inflight["lease_id"],
                            },
                        )
                    ]
                    if result["task_status"] == "done"
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-blocked:{inflight['task_id']}:{manual_gate['question_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "blocked",
                                "lease_id": inflight["lease_id"],
                                "update_type": "manual_gate_required",
                                "question_id": manual_gate["question_id"],
                                "blockers": [manual_gate["question_id"]],
                            },
                        )
                    ]
                    if manual_gate
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-blocked:{inflight['task_id']}:{permission_request['request_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "blocked",
                                "lease_id": inflight["lease_id"],
                                "update_type": "permission_request_required",
                                "request_id": permission_request["request_id"],
                                "blockers": [permission_request["request_id"]],
                            },
                        )
                    ]
                    if permission_request
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            (
                                f"backlog-integration-"
                                f"{result['task_status']}:{inflight['attempt_id']}"
                            ),
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": projected_task_status,
                                "lease_id": inflight["lease_id"],
                                "update_type": "integration_outcome",
                                "failure_category": result["failure_category"],
                                "retryable": result["retryable"],
                            },
                        )
                    ]
                    if (
                        outcome["validation_status"] == "accepted"
                        and result["task_status"] != "done"
                        and not manual_gate
                        and not permission_request
                    )
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            (
                                f"backlog-validation-"
                                f"{result['task_status']}:{inflight['attempt_id']}"
                            ),
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": projected_task_status,
                                "lease_id": inflight["lease_id"],
                                "update_type": "validation_outcome",
                                "failure_category": result[
                                    "failure_category"
                                ],
                                "retryable": result["retryable"],
                            },
                        )
                    ]
                    if (
                        outcome["validation_status"] != "accepted"
                        and not manual_gate
                        and not permission_request
                    )
                    else []
                ),
                *(
                    [
                        self._event(
                            "recovery_routed",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"recovery:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                "failure_category": result["failure_category"],
                                "next_attempt_id": next_attempt_id,
                                "recovery_action": retry_decision["action"],
                                "retry_decision_id": retry_decision[
                                    "decision_id"
                                ],
                            },
                        )
                    ]
                    if retry_allowed
                    else []
                ),
            ]
        if self.decision_binding is not None:
            runtime_events = [
                compact_event(event, result) for event in runtime_events
            ]
        canonical_events = self._append_events(inflight["step_id"], runtime_events)
        self._notify_canonical_events(inflight["step_id"], canonical_events)
        self._update_task_from_outcome(
            inflight["task_id"],
            outcome["validation_status"],
            result["failure_category"],
            retry_allowed,
            task_status=result["task_status"],
            manual_gate_question_id=manual_gate["question_id"] if manual_gate else None,
            permission_request_id=permission_request["request_id"]
            if permission_request
            else None,
        )
        self.state["steps"].append(
            {
                "step_id": inflight["step_id"],
                "step_status": "retry_routed" if retry_allowed else "processed",
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "attempt_number": inflight["attempt_number"],
                "validation_status": outcome["validation_status"],
                "failure_category": result["failure_category"],
                "retryable": result["retryable"],
                "retry_decision": retry_decision,
                "result": result,
            }
        )
        self._write_state()
        if self.decision_binding is not None:
            result.update(retire_transport(inflight.get("outbox_path")))
            self._write_state()
        if integration_transaction_active:
            self.state["integration_active"] = False
            self.state.pop("integration_attempt_id", None)
            self._write_state()
        post_integration_observation = self._observe_experiment_boundary(
            "post_integration"
        )
        if post_integration_observation is not None:
            result["post_integration_controller_observation"] = deepcopy(
                post_integration_observation
            )
            self._write_state()
        return result

    def _restore_inflight_worktree_if_missing(self, inflight):
        worktree_path = inflight.get("worktree_path")
        if (
            self.decision_binding is None
            or self.project_root is None
            or not worktree_path
            or Path(worktree_path).exists()
        ):
            return None
        recovered = restore_attempt_workspace(
            self.decision_binding,
            project_root=self.project_root,
            destination=worktree_path,
            run_id=self.output_dir.name,
            task_id=inflight["task_id"],
            attempt_id=inflight["attempt_id"],
            independent=self.independent_attempt_workspaces,
            events_path=self.events_path,
        )
        compact = {
            key: value
            for key, value in recovered.items()
            if key != "event_tail"
        }
        inflight["worktree_recovery"] = compact
        inflight["branch"] = None
        return compact

    def _apply_decomposition_result(self, inflight, runtime_result, result, outcome):
        task = self._task_by_id(inflight["task_id"])
        if task.get("task_kind") != "decompose_backlog":
            return []
        if outcome["validation_status"] != "accepted":
            result["decomposition_status"] = "not_applied"
            result["generated_task_ids"] = []
            return []
        try:
            context = self._read_planner_context(task)
            normalized = normalize_task_proposal(
                runtime_result.get("output", {}).get("task_proposal"),
                existing_task_ids={
                    item["task_id"]
                    for item in self.state["backlog"]["items"]
                },
                allowed_roles=context["available_agent_roles"],
                allowed_write_scopes=context["allowed_write_scopes"],
            )
        except ValueError as exc:
            result["decomposition_status"] = "rejected"
            result["generated_task_ids"] = []
            result["failure_category"] = "invalid_task_proposal"
            result["decomposition_error"] = str(exc)
            outcome["validation_status"] = "rejected"
            outcome["failure_category"] = "invalid_task_proposal"
            outcome["retryable"] = False
            return []

        decomposition_wave = task.get(
            "decomposition_wave",
            _decomposition_wave_from_task_id(task["task_id"]),
        )
        for generated_task in normalized["tasks"]:
            generated_task["generated_by_decomposition_task_id"] = task["task_id"]
            generated_task["decomposition_wave"] = decomposition_wave
        self.state["backlog"]["items"].extend(normalized["tasks"])
        self._record_decomposition_applied(
            task,
            normalized["generated_task_ids"],
            decomposition_wave,
        )
        result["decomposition_status"] = "applied"
        result["generated_task_ids"] = normalized["generated_task_ids"]
        result["generated_task_count"] = len(normalized["generated_task_ids"])
        semantic_escalation_events = [
            self._event(
                "semantic_escalation_required",
                "agent-scheduler",
                generated_task.get("recommended_role"),
                f"semantic-escalation:{generated_task['task_id']}",
                inflight["correlation_id"],
                {
                    "task_id": generated_task["task_id"],
                    "source_task_id": task["task_id"],
                    "risk_target": generated_task.get("risk_target"),
                    "reason": "semantic_escalation_required",
                    "recommended_role": generated_task.get("recommended_role"),
                    "semantic_escalation_status": generated_task.get("semantic_escalation_status"),
                },
            )
            for generated_task in normalized["tasks"]
            if generated_task.get("risk_target") == "L3"
        ]
        return [
            self._event(
                "backlog_updated",
                "agent-scheduler",
                None,
                f"backlog-decomposition:{inflight['attempt_id']}",
                inflight["correlation_id"],
                {
                    "task_id": inflight["task_id"],
                    "attempt_id": inflight["attempt_id"],
                    "lease_id": inflight["lease_id"],
                    "task_status": "done",
                    "update_type": "decomposition_applied",
                    "generated_task_ids": normalized["generated_task_ids"],
                },
            )
        ] + semantic_escalation_events

    def _read_planner_context(self, task):
        context_path = task.get("planner_context_path")
        if not context_path:
            return {
                "available_agent_roles": None,
                "allowed_write_scopes": None,
            }
        return json.loads(Path(context_path).read_text(encoding="utf-8"))

    def _milestone_state(self, milestone_id):
        milestones = self.state.setdefault("milestones", {})
        return milestones.setdefault(
            milestone_id,
            {
                "milestone_id": milestone_id,
                "milestone_status": "active",
                "decomposition_status": "idle",
                "decomposition_wave_count": 0,
                "current_decomposition_task_id": None,
                "generated_task_ids": [],
            },
        )

    def _record_decomposition_applied(self, task, generated_task_ids, decomposition_wave):
        milestone = self._milestone_state(task["milestone_id"])
        generated = list(milestone.get("generated_task_ids", []))
        for task_id in generated_task_ids:
            if task_id not in generated:
                generated.append(task_id)
        milestone.update(
            {
                "milestone_status": "active",
                "decomposition_status": "batch_active",
                "decomposition_wave_count": max(
                    milestone.get("decomposition_wave_count", 0),
                    decomposition_wave,
                ),
                "current_decomposition_task_id": task["task_id"],
                "generated_task_ids": generated,
            }
        )

    def _ensure_integration_baseline(self):
        baseline = ensure_integration_baseline_worktree(
            self.project_root,
            self.output_dir,
            base_ref=self.initial_integration_base_ref,
            independent=self.independent_attempt_workspaces,
        )
        self.state["integration_baseline"] = baseline
        return baseline

    def _register_experiment_attempt_launch(
        self,
        invocation_context,
        *,
        worktree_path,
        attempt_id,
        taskpack_id,
    ):
        if worktree_path is None:
            raise ValueError(
                "experiment provider attempt requires a workspace"
            )
        context = self.experiment_runtime_context
        configuration = context.get("sandbox_configuration")
        if not isinstance(configuration, dict):
            raise ValueError(
                "experiment sandbox configuration is unavailable"
            )
        required = {
            "runtime_views",
            "library_views",
            "credential_mounts",
            "environment",
            "canary_path",
        }
        if set(configuration) != required:
            raise ValueError(
                "experiment sandbox configuration fields are invalid"
            )
        lifecycle_id = _experiment_reference_id(
            f"worker-{attempt_id}"
        )
        lifecycle_root = experiment_lifecycle_authority_root(
            context["authority_root"],
            lifecycle_id,
        )
        descriptor = build_provider_sandbox_descriptor(
            worktree_path,
            runtime_views=configuration["runtime_views"],
            library_views=configuration["library_views"],
            credential_mounts=configuration["credential_mounts"],
            environment=configuration["environment"],
            network_policy=context["model_policy"][
                "network_policy"
            ],
            repository_identity={
                field: context["repository_identity"][field]
                for field in (
                    "commit",
                    "tree",
                    "git_object_format",
                )
            },
            forbidden_paths=[configuration["canary_path"]],
        )
        sandbox_reference = publish_provider_sandbox_reference(
            context["authority_root"],
            descriptor,
            configuration["canary_path"],
            reference_id=f"{lifecycle_id}-sandbox",
        )
        publish_experiment_launch_registration(
            context["authority_root"],
            lifecycle_root,
            experiment_run_id=context["experiment_run_id"],
            protocol_sha256=context["protocol_sha256"],
            run_manifest_sha256=context["run_manifest_sha256"],
            mode=context["mode"],
            usage_stage=invocation_context["usage_stage"],
            taskpack_id=taskpack_id,
            workspace_root=worktree_path,
            sandbox_reference=sandbox_reference,
            controller_reference=context["controller_reference"],
            model_policy=context["model_policy"],
        )
        updated = deepcopy(invocation_context)
        updated.update(
            {
                "run_id": context["experiment_run_id"],
                "experiment_sandbox_reference": sandbox_reference,
                "experiment_sandbox_required": True,
                "experiment_authority_root": context["authority_root"],
                "experiment_controller_reference": deepcopy(
                    context["controller_reference"]
                ),
                "experiment_controller_required": True,
                "model_invocation_authority_root": str(lifecycle_root),
                "model": context["model_policy"]["model"],
                "reasoning_profile": context["model_policy"][
                    "reasoning_profile"
                ],
            }
        )
        if context.get("resource_envelope_binding") is not None:
            updated.update(
                {
                    "experiment_mode": context["mode"],
                    "resource_envelope_binding": deepcopy(
                        context["resource_envelope_binding"]
                    ),
                    "resource_envelope_required": True,
                    "resource_project_id": context[
                        "resource_project_id"
                    ],
                    "resource_hierarchy_reference": deepcopy(
                        context["resource_hierarchy_reference"]
                    ),
                }
            )
        return updated

    def _stage_experiment_provider_io(
        self,
        worktree_path,
        attempt_id,
        context_fields,
    ):
        workspace = Path(worktree_path).resolve()
        git_dir = workspace / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise ValueError(
                "experiment provider workspace requires a standalone .git directory"
            )
        attempt_key = hashlib.sha256(
            attempt_id.encode("utf-8")
        ).hexdigest()[:16]
        provider_io_dir = (
            git_dir / "agentteam-provider-io" / attempt_key
        )
        provider_io_dir.mkdir(parents=True, mode=0o700, exist_ok=False)

        staged = {}
        for field, filename in (
            ("role_context_path", "role-context.json"),
            ("repo_context_path", "repo-context.json"),
        ):
            source_value = context_fields.get(field)
            if not source_value:
                continue
            source = Path(source_value)
            if source.is_symlink() or not source.is_file():
                raise ValueError(
                    f"experiment provider {field} is not a regular file"
                )
            destination = provider_io_dir / filename
            destination.write_bytes(source.read_bytes())
            staged[field] = str(destination)

        result_path = provider_io_dir / "result.json"
        result_path.write_bytes(b"")
        staged["provider_result_path"] = str(result_path)
        return staged

    def _record_integration_baseline_result(self, integration, head_sha):
        self.state["integration_baseline"] = {
            "integration_baseline_status": "ready",
            "integration_baseline_branch": integration.get(
                "integration_baseline_branch"
            ),
            "integration_baseline_worktree_path": integration.get(
                "integration_baseline_worktree_path"
            ),
            "integration_baseline_head_sha": head_sha,
        }

    def _verified_backlog_transition(self, inflight, result, patch_path, outcome):
        if outcome["validation_status"] != "accepted":
            return {
                "task_status": "retryable" if outcome["retryable"] else "blocked",
                "completion_policy": "worker_validation",
                "failure_category": outcome["failure_category"],
                "retryable": outcome["retryable"],
                "verified_integration_head_sha": None,
            }
        if not patch_path:
            return {
                "task_status": "done",
                "completion_policy": "verified_noop",
                "failure_category": None,
                "retryable": False,
                "verified_integration_head_sha": (
                    result.get("integration_baseline_commit_sha")
                    or inflight.get("integration_base_sha")
                ),
            }
        if result.get("integration_status") == "preserved":
            return {
                "task_status": "blocked",
                "completion_policy": "accepted_patch_preserved_budget_stop",
                "failure_category": "integration_deferred_budget_stop",
                "retryable": False,
                "verified_integration_head_sha": None,
            }
        if not self.integrate_accepted_patch:
            return {
                "task_status": "blocked",
                "completion_policy": "verified_integration_required",
                "failure_category": "integration_not_requested",
                "retryable": False,
                "verified_integration_head_sha": None,
            }

        baseline = self.state.get("integration_baseline", {})
        verified_head = (
            result.get("integration_baseline_commit_sha")
            or baseline.get("integration_baseline_head_sha")
        )
        committed = (
            result.get("integration_baseline_commit_status") == "committed"
            and bool(result.get("integration_baseline_commit_sha"))
        )
        recovered_verified_commit = (
            result.get("integration_recovery_status") == "reused_existing"
            and result.get("integration_verification_status") == "passed"
            and bool(verified_head)
            and verified_head != inflight.get("integration_base_sha")
        )
        if committed or recovered_verified_commit:
            return {
                "task_status": "done",
                "completion_policy": "verified_integration_commit",
                "failure_category": None,
                "retryable": False,
                "verified_integration_head_sha": verified_head,
            }

        if result.get("integration_status") == "failed":
            failure_category = "integration_apply_failed"
            retryable = True
        elif result.get("integration_verification_status") == "failed":
            failure_category = "integration_verification_failed"
            retryable = result.get(
                "integration_verification_failure_reason"
            ) not in {
                "verification_addition_failed",
                "verification_addition_rejected",
            }
        elif result.get("integration_block_reason") == "evidence_incomplete":
            failure_category = "integration_evidence_incomplete"
            retryable = False
        else:
            failure_category = (
                result.get("integration_baseline_commit_reason")
                or result.get("integration_commit_reason")
                or "integration_not_verified"
            )
            retryable = False
        return {
            "task_status": "retryable" if retryable else "blocked",
            "completion_policy": "verified_integration_commit",
            "failure_category": failure_category,
            "retryable": retryable,
            "verified_integration_head_sha": None,
        }

    def _preserve_accepted_patch(
        self,
        inflight,
        result,
        patch_path,
        observation,
    ):
        result.update(
            {
                "integration_status": "preserved",
                "integration_block_reason": "experiment_budget_stop",
                "integration_preservation_reason": observation[
                    "controller_status"
                ],
            }
        )
        queue = upsert_integration_queue_item(self.output_dir, result)
        result.update(queue)
        return [
            self._event(
                "integration_deferred_by_experiment_budget",
                "agent-scheduler",
                inflight["agent_id"],
                f"integration-budget-deferred:{inflight['attempt_id']}",
                inflight["correlation_id"],
                {
                    "task_id": inflight["task_id"],
                    "attempt_id": inflight["attempt_id"],
                    "lease_id": inflight["lease_id"],
                    "patch_path": str(patch_path),
                    "controller_status": observation["controller_status"],
                    "budget_state": deepcopy(observation["budget_state"]),
                    **queue,
                },
            )
        ]

    def _integrate_accepted_result(self, inflight, result, patch_path, outcome):
        if outcome["validation_status"] != "accepted":
            return []
        if _integration_blocked_by_evidence(result, patch_path):
            result["integration_status"] = "blocked"
            result["integration_block_reason"] = "evidence_incomplete"
            return [
                self._event(
                    "evidence_incomplete",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"evidence-incomplete:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "evidence_level": result.get("evidence_level"),
                        "evidence_status": result.get("evidence_status"),
                        "missing_evidence": result.get("missing_evidence", []),
                        "trace_carrier": result.get("trace_carrier", []),
                    },
                ),
                self._event(
                    "integration_blocked_by_evidence",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-blocked-by-evidence:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "block_reason": "evidence_incomplete",
                        "patch_path": str(patch_path) if patch_path else None,
                        "evidence_level": result.get("evidence_level"),
                        "evidence_status": result.get("evidence_status"),
                        "missing_evidence": result.get("missing_evidence", []),
                        "trace_carrier": result.get("trace_carrier", []),
                    },
                ),
            ]
        acceptance = record_integration_acceptance(
            self.decision_binding,
            run_id=self.output_dir.name,
            task_id=inflight["task_id"],
            attempt_id=inflight["attempt_id"],
            created_at=self.clock.now(),
            evidence_refs=_acceptance_evidence_refs(result),
        )
        events = []
        if acceptance is not None:
            acceptance_id = acceptance["decision_id"]
            result["acceptance_decision_id"] = acceptance_id
            self.state.setdefault("acceptance_decisions", {})[
                inflight["attempt_id"]
            ] = acceptance_id
            self._write_state()
            events.append(
                self._event(
                    "acceptance_decision_recorded",
                    "verification-integration-controller",
                    inflight["agent_id"],
                    f"acceptance:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "acceptance_decision_id": acceptance_id,
                        "accepted_option": acceptance["selected_option"],
                    },
                )
            )
        if not patch_path:
            baseline_head = (
                self.state.get("integration_baseline", {}).get(
                    "integration_baseline_head_sha"
                )
                or inflight.get("integration_base_sha")
            )
            result.update(
                {
                    "integration_status": "verified_noop",
                    "integration_noop_status": "verified",
                    "integration_noop_reason": "accepted_result_has_no_patch",
                    "integration_baseline_commit_status": "unchanged",
                    "integration_baseline_commit_sha": baseline_head,
                    "integration_baseline_commit_reason": "verified_noop",
                }
            )
            integration_code_state = self._publish_verified_integration_code_state(
                inflight,
                result,
                acceptance,
                baseline_head,
            )
            if integration_code_state is not None:
                events.append(integration_code_state)
            events.append(
                self._event(
                    "integration_noop_verified",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-noop:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "integration_noop_status": "verified",
                        "integration_noop_reason": "accepted_result_has_no_patch",
                        "integration_baseline_commit_sha": baseline_head,
                    },
                )
            )
            return events
        if patch_path:
            queue = upsert_integration_queue_item(self.output_dir, result)
            result.update(queue)
            events.append(
                self._event(
                    "integration_queued",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-queued:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "patch_path": str(patch_path),
                        **queue,
                    },
                )
            )
        if self.integrate_accepted_patch and self.project_root and patch_path:
            try:
                integration = apply_patch_to_integration_baseline_worktree(
                    self.project_root,
                    self.output_dir,
                    patch_path,
                )
            except Exception as exc:
                integration = {
                    "integration_status": "failed",
                    "integration_failure_reason": "apply_failed",
                    "integration_error": str(exc),
                    "integration_base_ref": inflight.get("integration_base_ref"),
                    "integration_base_sha": inflight.get("integration_base_sha"),
                    "integration_baseline_branch": inflight.get(
                        "integration_baseline_branch"
                    ),
                    "integration_baseline_worktree_path": inflight.get(
                        "integration_baseline_worktree_path"
                    ),
                }
            result.update(integration)
            if integration.get("integration_status") != "applied":
                baseline_commit = skip_integration_baseline_commit("apply_failed")
                rollback = reset_integration_baseline_worktree(
                    inflight["integration_baseline_worktree_path"]
                )
                result.update(baseline_commit)
                result.update(rollback)
                self._record_integration_baseline_result(
                    integration,
                    inflight["integration_base_sha"],
                )
                events.append(
                    self._event(
                        "integration_blocked",
                        "agent-scheduler",
                        inflight["agent_id"],
                        f"integration-blocked:{inflight['attempt_id']}",
                        inflight["correlation_id"],
                        {
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            "block_reason": "apply_failed",
                            "patch_path": str(patch_path),
                            **integration,
                            **baseline_commit,
                            **rollback,
                        },
                    )
                )
                events.append(
                    self._event(
                        "integration_baseline_commit_evaluated",
                        "agent-scheduler",
                        inflight["agent_id"],
                        f"integration-baseline-commit:{inflight['attempt_id']}",
                        inflight["correlation_id"],
                        {
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            **baseline_commit,
                            **rollback,
                        },
                    )
                )
                if self.commit_verified_integration:
                    integration_commit = _integration_commit_from_baseline_result(
                        result
                    )
                    result.update(integration_commit)
                    events.append(
                        self._event(
                            "integration_commit_evaluated",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"integration-commit:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                **integration_commit,
                            },
                        )
                    )
                result.update(upsert_integration_queue_item(self.output_dir, result))
                return events
            events.append(
                self._event(
                    "patch_integrated",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"patch-integrated:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "patch_path": str(patch_path),
                        **integration,
                    },
                )
            )
            if self.integration_verification_command:
                verification = run_integration_verification(
                    self.integration_verification_command,
                    integration["integration_worktree_path"],
                )
                verification_additions = _runtime_verification_additions(
                    {"output": result.get("runtime_output", {})}
                )
                if verification["integration_verification_status"] == "passed":
                    additions = run_integration_verification_additions(
                        verification_additions,
                        integration["integration_worktree_path"],
                    )
                    verification.update(additions)
                    additions_status = additions[
                        "integration_verification_additions_status"
                    ]
                    if additions_status in {"failed", "rejected"}:
                        verification["integration_verification_status"] = "failed"
                        verification[
                            "integration_verification_failure_reason"
                        ] = f"verification_addition_{additions_status}"
                elif verification_additions:
                    verification.update(
                        {
                            "integration_verification_additions_status": "skipped",
                            "integration_verification_additions": [],
                            "integration_verification_additions_skip_reason": (
                                "primary_verification_failed"
                            ),
                        }
                    )
                result.update(verification)
                events.append(
                    self._event(
                        "integration_verified",
                        "agent-scheduler",
                        inflight["agent_id"],
                        f"integration-verified:{inflight['attempt_id']}",
                        inflight["correlation_id"],
                        {
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            **verification,
                        },
                    )
                )
                if verification["integration_verification_status"] == "passed":
                    baseline_commit = commit_integration_baseline_worktree(
                        integration["integration_baseline_worktree_path"],
                        inflight["task_id"],
                        inflight["attempt_id"],
                    )
                    result.update(baseline_commit)
                    self._record_integration_baseline_result(
                        integration,
                        baseline_commit["integration_baseline_commit_sha"],
                    )
                    events.append(
                        self._event(
                            "integration_baseline_commit_evaluated",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"integration-baseline-commit:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                **baseline_commit,
                            },
                        )
                    )
                else:
                    baseline_commit = skip_integration_baseline_commit(
                        "verification_failed"
                    )
                    rollback = reset_integration_baseline_worktree(
                        integration["integration_baseline_worktree_path"]
                    )
                    result.update(baseline_commit)
                    result.update(rollback)
                    self._record_integration_baseline_result(
                        integration,
                        integration["integration_base_sha"],
                    )
                    events.append(
                        self._event(
                            "integration_blocked",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"integration-blocked:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                "block_reason": "verification_failed",
                                "patch_path": str(patch_path),
                                **integration,
                                **verification,
                                **baseline_commit,
                                **rollback,
                            },
                        )
                    )
                    events.append(
                        self._event(
                            "integration_baseline_commit_evaluated",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"integration-baseline-commit:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                **baseline_commit,
                                **rollback,
                            },
                        )
                    )
            else:
                baseline_commit = skip_integration_baseline_commit(
                    "verification_not_requested"
                )
                rollback = reset_integration_baseline_worktree(
                    integration["integration_baseline_worktree_path"]
                )
                result.update(baseline_commit)
                result.update(rollback)
                self._record_integration_baseline_result(
                    integration,
                    integration["integration_base_sha"],
                )
                events.append(
                    self._event(
                        "integration_baseline_commit_evaluated",
                        "agent-scheduler",
                        inflight["agent_id"],
                        f"integration-baseline-commit:{inflight['attempt_id']}",
                        inflight["correlation_id"],
                        {
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            **baseline_commit,
                            **rollback,
                        },
                    )
                )
        if self.commit_verified_integration:
            if result.get("integration_baseline_commit_status") != "not_requested":
                integration_commit = _integration_commit_from_baseline_result(result)
            else:
                integration_commit = evaluate_integration_commit(
                    result,
                    inflight["task_id"],
                    inflight["attempt_id"],
                )
            result.update(integration_commit)
            events.append(
                self._event(
                    "integration_commit_evaluated",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-commit:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        **integration_commit,
                    },
                )
            )
        integration_code_state = self._publish_verified_integration_code_state(
            inflight,
            result,
            acceptance,
            result.get("integration_baseline_commit_sha"),
        )
        if integration_code_state is not None:
            events.append(integration_code_state)
        if patch_path:
            result.update(upsert_integration_queue_item(self.output_dir, result))
        return events

    def _publish_verified_integration_code_state(
        self,
        inflight,
        result,
        acceptance,
        commit_sha,
    ):
        if acceptance is None or self.project_root is None or not commit_sha:
            return None
        verified = (
            result.get("integration_status") == "verified_noop"
            or result.get("integration_baseline_commit_status") == "committed"
            or (
                result.get("integration_recovery_status") == "reused_existing"
                and result.get("integration_verification_status") == "passed"
            )
        )
        if not verified:
            return None
        code_state = publish_integration_code_state(
            self.decision_binding,
            project_root=self.project_root,
            source_repository=(
                result.get("integration_baseline_worktree_path")
                or self.project_root
            ),
            decision_id=acceptance["decision_id"],
            commit_sha=commit_sha,
            run_id=self.output_dir.name,
            task_id=inflight["task_id"],
            attempt_id=inflight["attempt_id"],
            created_at=self.clock.now(),
        )
        result.update(code_state)
        return self._event(
            "code_state_published",
            "verification-integration-controller",
            inflight["agent_id"],
            f"integration-code-state:{inflight['attempt_id']}",
            inflight["correlation_id"],
            {
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "lease_id": inflight["lease_id"],
                "code_state_decision_id": acceptance["decision_id"],
                **code_state,
            },
        )

    def _lease_expired(self, inflight):
        now = _parse_utc_timestamp(self.clock.now())
        lease_expires_at = _parse_utc_timestamp(inflight["lease_expires_at"])
        return now >= lease_expires_at

    def _timeout_runtime_result(self, inflight):
        return {
            "result_status": "timed_out",
            "changed_files": [],
            "output": {
                "adapter": "two_phase_scheduler",
                "error": "lease_timeout",
                "lease_expires_at": inflight["lease_expires_at"],
                "message_id": inflight["message_id"],
            },
        }

    def _ready_tasks(self):
        inflight_task_ids = {
            attempt["task_id"]
            for attempt in self.state["inflight_attempts"]
        }
        done_by_id = {
            item["task_id"]: item.get("backlog_status") == "done"
            for item in self.state["backlog"]["items"]
        }
        ready = []
        for task in self.state["backlog"]["items"]:
            if task["task_id"] in inflight_task_ids:
                continue
            if task.get("backlog_status") != "ready":
                continue
            if task.get("blockers"):
                continue
            if not all(done_by_id.get(dep_id, False) for dep_id in task.get("depends_on", [])):
                continue
            ready.append(task)
        return ready

    def _mark_inflight_agents_busy(self, agent_pool):
        inflight_agent_ids = {
            attempt["agent_id"]
            for attempt in self.state["inflight_attempts"]
        }
        for agent in agent_pool["agents"]:
            if agent["agent_id"] in inflight_agent_ids:
                agent["status"] = "busy"

    def _mark_unavailable_agents(self, agent_pool):
        for agent in agent_pool["agents"]:
            if agent["agent_id"] in self.unavailable_agent_ids:
                agent["status"] = "unavailable"

    def _unavailable_agent_ids_for_role(self, agent_pool, role):
        return [
            agent["agent_id"]
            for agent in agent_pool["agents"]
            if agent.get("role") == role
            and agent["agent_id"] in self.unavailable_agent_ids
        ]

    def _status_without_dispatch(self):
        controller_status = self.state.get("experiment_controller_status")
        if controller_status in {
            "budget_draining",
            "budget_stopped",
            "interrupted",
        }:
            return controller_status
        if self.state["inflight_attempts"]:
            return "waiting"
        if self._ready_tasks():
            return "running"
        return "idle"

    def _prepare_experiment_dispatch(self):
        if self.experiment_controller is None:
            return None
        integration_recovery = self._recover_experiment_integration_state()
        if integration_recovery == "recovery_required":
            self.state["scheduler_status"] = "waiting"
            self._write_state()
            return self._experiment_dispatch_result(
                "integration_recovery_required"
            )
        reconciliations = self._reconcile_experiment_inflight()
        self.experiment_controller = load_experiment_controller(
            self.experiment_controller_reference,
            monotonic=self.experiment_controller_monotonic,
        )
        self._record_experiment_controller_snapshot()
        blocking = [
            item
            for item in reconciliations
            if item["reconciliation_status"]
            in {"live", "open_ambiguous", "service_stop_required"}
        ]
        if blocking:
            self.state["scheduler_status"] = "waiting"
            self._write_state()
            return self._experiment_dispatch_result(
                "invocation_reconciliation_pending",
                reconciliations=blocking,
            )
        if (
            self.experiment_controller.controller_status == "interrupted"
            and self.resume_interrupted_experiment
        ):
            self.experiment_controller.resume_interrupted(
                reference=self.experiment_controller_reference
            )
            self._record_experiment_controller_snapshot()
        if self.state["inflight_attempts"]:
            return None
        observation = self._observe_experiment_boundary(
            "pre_provider_launch"
        )
        if observation["allow_provider_launch"]:
            return None
        if not self.state["inflight_attempts"]:
            observation = self._observe_experiment_boundary(
                "post_integration"
            )
        self.state["scheduler_status"] = observation["controller_status"]
        self._write_state()
        return self._experiment_dispatch_result(
            observation["controller_status"]
        )

    def _recover_experiment_integration_state(self):
        if not self.state.get("integration_active"):
            return "not_required"
        attempt_id = self.state.get("integration_attempt_id")
        completed = any(
            (
                step.get("attempt_id") == attempt_id
                or step.get("result", {}).get("attempt_id") == attempt_id
            )
            and isinstance(step.get("result"), dict)
            for step in self.state.get("steps", [])
        )
        if not completed:
            return "recovery_required"
        self.state["integration_active"] = False
        self.state.pop("integration_attempt_id", None)
        self._write_state()
        return "completed_transaction_recovered"

    def _completed_attempt_result(self, inflight):
        attempt_id = inflight.get("attempt_id")
        for step in reversed(self.state.get("steps", [])):
            if (
                (
                    step.get("attempt_id") == attempt_id
                    or step.get("step_id") == inflight.get("step_id")
                    or step.get("result", {}).get("attempt_id")
                    == attempt_id
                )
                and isinstance(step.get("result"), dict)
            ):
                return deepcopy(step["result"])
        return None

    def _empty_collect_result(self):
        return {
            "collect_status": "idle",
            "collected_task_ids": [],
            "collected_count": 0,
            "inflight_count": len(self.state["inflight_attempts"]),
            "results": [],
        }

    def _experiment_dispatch_result(self, status, *, reconciliations=None):
        result = {
            "dispatch_status": status,
            "dispatched_task_ids": [],
            "dispatch_count": 0,
            "inflight_count": len(self.state["inflight_attempts"]),
            "experiment_controller_status": self.state.get(
                "experiment_controller_status"
            ),
            "experiment_budget_state": deepcopy(
                self.state.get("experiment_budget_state")
            ),
        }
        if reconciliations is not None:
            result["invocation_reconciliations"] = deepcopy(reconciliations)
        return result

    def _validate_trusted_task_controller_authority(self, task):
        if self.experiment_controller is not None:
            return
        if (
            task.get("experiment_controller_reference") is not None
            or task.get("experiment_controller_required") is True
        ):
            raise ExperimentControllerIntegrityError(
                "task experiment controller authority is not trusted "
                "scheduler configuration"
            )

    def _reconcile_experiment_inflight(self):
        reconciliations = []
        for inflight in self.state["inflight_attempts"]:
            self._import_worker_lifecycles(inflight)
            reconciliation = reconcile_orphaned_invocation(
                self._inflight_invocation_authority_root(inflight),
                inflight,
                fence_assessor=self.invocation_fence_assessor,
                service_stopper=self.invocation_service_stopper,
            )
            inflight["invocation_reconciliation"] = reconciliation
            reconciliations.append(reconciliation)
        return reconciliations

    def _observe_experiment_boundary(self, boundary):
        if self.experiment_controller is None:
            return None
        observation = self.experiment_controller.observe_boundary(
            boundary,
            scheduler_inflight=len(self.state["inflight_attempts"]),
            integration_active=self.state.get("integration_active", False),
            open_invocation_ids=self._open_experiment_invocation_ids(),
        )
        self._record_experiment_controller_snapshot()
        return {
            **observation,
            "budget_state": deepcopy(self.state["experiment_budget_state"]),
        }

    def _record_experiment_controller_snapshot(self):
        if self.experiment_controller is None:
            return
        snapshot = self.experiment_controller.snapshot()
        self.state["experiment_controller_status"] = snapshot[
            "controller_status"
        ]
        self.state["experiment_budget_state"] = deepcopy(
            snapshot["budget_state"]
        )

    def _open_experiment_invocation_ids(self):
        invocation_root = self.output_dir / "model_invocations"
        if not invocation_root.exists():
            return []
        open_ids = []
        for started_path in sorted(invocation_root.glob("*/started.json")):
            if started_path.with_name("terminal.json").is_file():
                continue
            start = _read_json_file_if_exists(started_path)
            if start is not None and start.get("invocation_id"):
                open_ids.append(start["invocation_id"])
        return open_ids

    def _ensure_decomposition_task(self):
        if not self.auto_decompose:
            return
        if self.state["inflight_attempts"] or self._ready_tasks():
            return
        milestone_id = self.decomposition_milestone_id
        decomposition_tasks = self._decomposition_tasks_for_milestone(milestone_id)
        if any(not _is_terminal_backlog_status(task) for task in decomposition_tasks):
            return
        if decomposition_tasks:
            latest = decomposition_tasks[-1]
            if latest.get("backlog_status") != "done":
                self._mark_milestone_terminal(milestone_id, "blocked")
                return
            if not self._generated_batch_terminal(latest["task_id"]):
                milestone = self._milestone_state(milestone_id)
                milestone["decomposition_status"] = "batch_active"
                return
        if len(decomposition_tasks) >= self.decomposition_max_waves:
            self._mark_milestone_terminal(
                milestone_id,
                self._terminal_milestone_status(milestone_id),
            )
            return
        decomposition_wave = len(decomposition_tasks) + 1
        task_id = f"DECOMPOSE-{milestone_id}-{decomposition_wave:03d}"
        planner_context_path = self._write_planner_context(task_id)
        self.state["backlog"]["items"].append(
            {
                "task_id": task_id,
                "task_kind": "decompose_backlog",
                "milestone_id": milestone_id,
                "decomposition_wave": decomposition_wave,
                "objective": (
                    "Generate the next bounded executable backlog tasks for "
                    f"{milestone_id}."
                ),
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "read_scope": ["."],
                "write_scope": [],
                "required_role": self.decomposition_planner_role,
                "default_worker_role": self.decomposition_default_worker_role,
                "planner_context_path": str(planner_context_path),
                "allowed_read_scopes": self.decomposition_allowed_read_scopes,
                "allowed_write_scopes": self.decomposition_allowed_write_scopes,
                "blockers": [],
            }
        )
        milestone = self._milestone_state(milestone_id)
        milestone.update(
            {
                "milestone_status": "active",
                "decomposition_status": "decomposition_ready",
                "decomposition_wave_count": max(
                    milestone.get("decomposition_wave_count", 0),
                    decomposition_wave,
                ),
                "current_decomposition_task_id": task_id,
            }
        )

    def _write_planner_context(self, task_id):
        context = build_planner_context(
            _read_json(self.agent_pool_path),
            self.state,
            milestone_id=self.decomposition_milestone_id,
            default_worker_role=self.decomposition_default_worker_role,
            allowed_read_scopes=self.decomposition_allowed_read_scopes,
            allowed_write_scopes=self.decomposition_allowed_write_scopes,
            context_artifact_paths=self.decomposition_context_artifact_paths,
            context_artifact_excerpt_chars=self.decomposition_context_excerpt_chars,
        )
        context_path = self.output_dir / "planner_contexts" / f"{task_id}.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
        return context_path

    def _task_by_id(self, task_id):
        for task in self.state["backlog"]["items"]:
            if task["task_id"] == task_id:
                return task
        raise ValueError(f"task not found in two-phase scheduler state: {task_id}")

    def _decomposition_tasks_for_milestone(self, milestone_id):
        return sorted(
            [
                task
                for task in self.state["backlog"]["items"]
                if task.get("task_kind") == "decompose_backlog"
                and task.get("milestone_id") == milestone_id
            ],
            key=lambda task: task.get(
                "decomposition_wave",
                _decomposition_wave_from_task_id(task["task_id"]),
            ),
        )

    def _generated_batch_terminal(self, decomposition_task_id):
        generated_tasks = [
            task
            for task in self.state["backlog"]["items"]
            if task.get("generated_by_decomposition_task_id") == decomposition_task_id
        ]
        return all(_is_terminal_backlog_status(task) for task in generated_tasks)

    def _terminal_milestone_status(self, milestone_id):
        generated_ids = self._milestone_state(milestone_id).get("generated_task_ids", [])
        generated_tasks = [
            self._task_by_id(task_id)
            for task_id in generated_ids
            if any(item["task_id"] == task_id for item in self.state["backlog"]["items"])
        ]
        if any(task.get("backlog_status") == "blocked" for task in generated_tasks):
            return "blocked"
        return "completed"

    def _mark_milestone_terminal(self, milestone_id, milestone_status):
        milestone = self._milestone_state(milestone_id)
        milestone["milestone_status"] = milestone_status
        milestone["decomposition_status"] = "max_waves_reached"
        milestone["terminal_reason"] = "max_waves_reached"

    def _update_task_from_outcome(
        self,
        task_id,
        validation_status,
        failure_category,
        retry_allowed,
        task_status=None,
        manual_gate_question_id=None,
        permission_request_id=None,
    ):
        task = self._task_by_id(task_id)
        if task_status == "done":
            task["backlog_status"] = "done"
            task["blockers"] = []
        elif task_status == "ready" or retry_allowed:
            task["backlog_status"] = "ready"
            task["blockers"] = []
        else:
            task["backlog_status"] = "blocked"
            task["blockers"] = [
                manual_gate_question_id
                or permission_request_id
                or failure_category
                or "validation_rejected"
            ]

    def _next_attempt_number(self, task_id):
        attempt_numbers = [
            item.get("attempt_number", 1)
            for item in [
                *self.state["steps"],
                *self.state["inflight_attempts"],
            ]
            if item["task_id"] == task_id
        ]
        return max(attempt_numbers, default=0) + 1

    def _retry_handoff_for_task(self, task_id):
        for step in reversed(self.state.get("steps", [])):
            if (
                step.get("task_id") != task_id
                or step.get("step_status") != "retry_routed"
            ):
                continue
            result = step.get("result")
            if not isinstance(result, dict):
                continue
            runtime_output = result.get("runtime_output")
            operator_summary = (
                runtime_output.get("operator_summary")
                if isinstance(runtime_output, dict)
                else None
            )
            operator_summary = (
                operator_summary if isinstance(operator_summary, dict) else {}
            )
            return {
                "schema_version": "retry_handoff.v1",
                "prior_attempt_id": result.get("attempt_id"),
                "failure_category": result.get("failure_category"),
                "validation_status": result.get("validation_status"),
                "evidence_status": result.get("evidence_status"),
                "missing_evidence": [
                    str(item)[:1000]
                    for item in result.get("missing_evidence", [])[:8]
                ],
                "changed_files": list(result.get("changed_files", []))[:128],
                "code_state_ref": result.get("code_state_ref"),
                "code_state_commit_sha": result.get("code_state_commit_sha"),
                "evidence_path": result.get("evidence_path"),
                "evidence_sha256": result.get("evidence_sha256"),
                "verification_summary": str(
                    operator_summary.get("verification_summary") or ""
                )[:2000],
                "recommended_action": str(
                    operator_summary.get("next_steps") or ""
                )[:2000],
                "retry_decision": result.get("retry_decision"),
            }
        return None

    def _next_step_id(self, task_id):
        step_number = len(self.state["steps"]) + len(self.state["inflight_attempts"]) + 1
        return f"STEP-{step_number:04d}-{task_id}"

    def _append_events(self, step_id, events):
        return append_canonical_events(
            self.events_path,
            events,
            run_id=self.run_id,
            step_id=step_id,
        )

    def _import_worker_lifecycles(self, inflight):
        authority_root = self._inflight_invocation_authority_root(
            inflight
        )
        imported = []
        for started_path, start in _matching_invocation_starts(
            authority_root,
            inflight,
        ):
            imported.extend(
                import_model_invocation_lifecycle(
                    self.events_path,
                    started_path,
                    actor="agent-scheduler",
                    run_id=start.get("run_id") or self.run_id,
                    step_id=inflight["step_id"],
                    source_root=authority_root,
                )
            )
        return imported

    def _inflight_invocation_authority_root(self, inflight):
        declared = inflight.get("model_invocation_authority_root")
        if declared is None:
            return self.output_dir
        authority_root = Path(declared).resolve()
        if self.experiment_runtime_context is None:
            if authority_root != self.output_dir.resolve():
                raise ValueError(
                    "non-experiment invocation authority root changed"
                )
            return authority_root
        experiment_authority = Path(
            self.experiment_runtime_context["authority_root"]
        ).resolve()
        if not authority_root.is_relative_to(experiment_authority):
            raise ValueError(
                "experiment invocation authority escapes authority root"
            )
        return authority_root

    def _notify_canonical_events(self, step_id, events):
        if not self.notification_sink:
            return []
        telemetry_events = []
        allowed_event_types = getattr(
            self.notification_sink,
            "allowed_event_types",
            DEFAULT_NOTIFICATION_EVENT_TYPES,
        )
        for event in events:
            if event.get("event_type") not in allowed_event_types:
                continue
            try:
                result = self.notification_sink.notify(
                    event,
                    {"run_dir": str(self.output_dir)},
                )
            except Exception as exc:
                result = [
                    self._event(
                        "notification_failed",
                        "agent-notifier",
                        None,
                        f"notification:{event['event_id']}:failed",
                        event["correlation_id"],
                        {
                            "provider": "unknown",
                            "source_event_type": event.get("event_type"),
                            "source_event_id": event.get("event_id"),
                            "source_event_sequence": event.get("sequence"),
                            "notification_status": "failed",
                            "error_class": exc.__class__.__name__,
                            "error_summary": str(exc)[:300],
                        },
                    )
                ]
            if isinstance(result, dict):
                telemetry_events.append(self._notification_event_from_spec(result, event))
            elif isinstance(result, list):
                telemetry_events.extend(
                    self._notification_event_from_spec(item, event)
                    for item in result
                    if isinstance(item, dict)
                )
        if telemetry_events:
            return self._append_events(step_id, telemetry_events)
        return []

    def _emit_run_event_once(self, event_type, payload):
        run_event_ids = self.state.setdefault("run_event_ids", {})
        if run_event_ids.get(event_type):
            return []
        replayed_event_id = self._existing_run_event_id(event_type)
        if replayed_event_id:
            run_event_ids[event_type] = replayed_event_id
            self._write_state()
            return []
        canonical = self._append_events(
            "STEP-RUN",
            [
                self._event(
                    event_type,
                    "agent-scheduler",
                    None,
                    f"{event_type}:{self.run_id}",
                    f"run:{self.run_id}",
                    payload,
                )
            ],
        )
        run_event_ids[event_type] = canonical[0]["event_id"]
        self._write_state()
        self._notify_canonical_events("STEP-RUN", canonical)
        return canonical

    def _existing_run_event_id(self, event_type):
        if not self.events_path.is_file():
            return None
        expected_key = f"{event_type}:{self.run_id}"
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                event.get("event_type") == event_type
                and event.get("idempotency_key") == expected_key
            ):
                return event.get("event_id")
        return None

    def _run_event_payload(self, run_status, extra=None):
        summary = self.summary()
        payload = {
            "run_status": run_status,
            "scheduler_status": self.state.get("scheduler_status"),
            "processed_task_count": len(summary["processed_task_ids"]),
            "inflight_count": summary["inflight_count"],
            "inactive_inflight_count": summary["inactive_inflight_count"],
            "step_count": len(self.state["steps"]),
        }
        if run_status != "running":
            operator_report = _operator_report_from_state(self.state)
            if run_status == "completed" and self.decision_binding is not None:
                report_artifact = publish_operator_report(
                    self.decision_binding,
                    self.output_dir,
                    operator_report,
                    created_at=self.clock.now(),
                )
                payload["operator_report_artifact"] = report_artifact
                payload["artifact_retention"] = apply_terminal_retention(
                    self.decision_binding,
                    self.output_dir,
                    self.state,
                    project_root=self.project_root,
                )
                compact_scheduler_state(self.state, report_artifact)
                self._write_state()
            elif operator_report["task_reports"]:
                payload["operator_report"] = operator_report
        if extra:
            payload.update(extra)
        return payload

    def _notification_event_from_spec(self, spec, source_event):
        if "time" in spec:
            return spec
        return self._event(
            spec["event_type"],
            spec.get("actor", "agent-notifier"),
            spec.get("target_agent_id"),
            spec.get("idempotency_key", f"notification:{source_event['event_id']}"),
            spec.get("correlation_id", source_event["correlation_id"]),
            spec.get("payload", {}),
        )

    def _event(self, event_type, actor, target_agent_id, idempotency_key, correlation_id, payload):
        decision_id = (
            payload.get("code_state_decision_id")
            or self._decision_for_task(payload.get("task_id"))
        )
        if event_type in {
            "acceptance_decision_recorded",
            "integration_noop_verified",
            "integration_queued",
            "patch_integrated",
            "integration_verified",
            "integration_commit_evaluated",
            "integration_baseline_commit_evaluated",
            "integration_blocked",
        }:
            acceptance_id = self.state.get("acceptance_decisions", {}).get(
                payload.get("attempt_id")
            )
            if acceptance_id is not None:
                decision_id = acceptance_id
        return _event(
            0,
            self.clock.now(),
            event_type,
            actor,
            target_agent_id,
            idempotency_key,
            correlation_id,
            payload,
            decision_id=decision_id,
        )

    def _decision_for_task(self, task_id=None):
        return inherited_decision_id(self.decision_binding, task_id)

    def _load_or_create_state(self):
        if self.state_path.exists():
            state = _read_json(self.state_path)
            state.setdefault("max_attempts", self.max_attempts)
            state.setdefault("lease_timeout_seconds", self.lease_timeout_seconds)
            state.setdefault("milestones", {})
            state.setdefault("integration_baseline", {})
            state.setdefault("integration_active", False)
            state.setdefault("acceptance_decisions", {})
            return state
        return {
            "scheduler_status": "initialized",
            "max_attempts": self.max_attempts,
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "backlog": _read_json(self.backlog_path),
            "steps": [],
            "inflight_attempts": [],
            "milestones": {},
            "integration_baseline": {},
            "integration_active": False,
            "acceptance_decisions": {},
        }

    def _bind_experiment_controller(self):
        persisted_reference = self.state.get(
            "experiment_controller_reference"
        )
        if persisted_reference is not None:
            persisted_reference = validate_experiment_controller_reference(
                persisted_reference
            )
            if (
                self.experiment_controller_reference is not None
                and persisted_reference != self.experiment_controller_reference
            ):
                raise ExperimentControllerIntegrityError(
                    "scheduler experiment controller reference changed on restart"
                )
            self.experiment_controller_reference = persisted_reference
            self.experiment_controller_required = True
        if self.experiment_controller_reference is None:
            if (
                self.experiment_controller_required
                or self.state.get("experiment_controller_required") is True
            ):
                raise ExperimentControllerIntegrityError(
                    "required scheduler experiment controller is unavailable"
                )
            self.state.setdefault("experiment_controller_required", False)
            return
        self.experiment_controller = load_experiment_controller(
            self.experiment_controller_reference,
            monotonic=self.experiment_controller_monotonic,
        )
        self.experiment_controller_required = True
        self.state["experiment_controller_reference"] = deepcopy(
            self.experiment_controller_reference
        )
        self.state["experiment_controller_required"] = True
        self._record_experiment_controller_snapshot()
        self._write_state()

    def _bind_experiment_runtime_context(self):
        if self.experiment_runtime_context is None:
            return
        context = self.experiment_runtime_context
        expected = {
            "experiment_run_id": context["experiment_run_id"],
            "experiment_run_manifest_sha256": context[
                "run_manifest_sha256"
            ],
            "experiment_target_path_sha256": hashlib.sha256(
                str(self.output_dir.resolve()).encode("utf-8")
            ).hexdigest(),
        }
        for key, value in expected.items():
            persisted = self.state.get(key)
            if persisted is not None and persisted != value:
                raise ExperimentControllerIntegrityError(
                    "scheduler experiment runtime binding changed on restart"
                )
            self.state[key] = value

    def _write_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(self.state, sort_keys=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            descriptor = os.open(
                self.state_path.parent,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _apply_run_stop_request(self):
        request = read_run_stop_request(self.output_dir)
        if not request:
            return None
        stop_status = request.get("stop_status") or "stopped"
        previous_status = self.state.get("scheduler_status")
        if previous_status != stop_status and "previous_scheduler_status" not in self.state:
            self.state["previous_scheduler_status"] = previous_status
        self.state["scheduler_status"] = stop_status
        for key in ("stop_requested_at", "stop_operator", "stop_mode", "stop_request_path"):
            if request.get(key):
                self.state[key] = request[key]
        self._write_state()
        return request

    def _stopped_tick_result(self):
        return {
            "tick_status": self.state.get("scheduler_status") or "stopped",
            "collect": self._stopped_collect_result(),
            "dispatch": self._stopped_dispatch_result(),
            "inflight_count": 0,
            "inactive_inflight_count": self._inactive_inflight_count(),
            "processed_task_ids": self.summary()["processed_task_ids"],
        }

    def _stopped_collect_result(self):
        return {
            "collect_status": "stopped",
            "collected_task_ids": [],
            "collected_count": 0,
            "inflight_count": 0,
            "inactive_inflight_count": self._inactive_inflight_count(),
            "results": [],
        }

    def _stopped_dispatch_result(self):
        return {
            "dispatch_status": "stopped",
            "dispatched_task_ids": [],
            "dispatch_count": 0,
            "inflight_count": 0,
            "inactive_inflight_count": self._inactive_inflight_count(),
        }

    def _active_inflight_count(self):
        if self._run_stop_status_is_terminal():
            return 0
        return len(self.state["inflight_attempts"])

    def _inactive_inflight_count(self):
        if not self._run_stop_status_is_terminal():
            return 0
        return len(self.state["inflight_attempts"])

    def _run_stop_status_is_terminal(self):
        return self.state.get("scheduler_status") in _STOP_SCHEDULER_STATUSES


def run_two_phase_scheduler_loop(*args, max_ticks=100, poll_interval_seconds=0.02, **kwargs):
    scheduler = TwoPhaseFileScheduler(*args, **kwargs)
    return scheduler.run_until_idle(
        max_ticks=max_ticks,
        poll_interval_seconds=poll_interval_seconds,
    )


def supported_worker_invocation_inventory():
    return deepcopy(list(SUPPORTED_WORKER_INVOCATION_INVENTORY))


def _worker_invocation_context(
    agent_pool,
    backlog,
    task,
    agent,
    *,
    run_id,
    runtime_execution_session_id,
    lease_id,
    output_dir,
    project_root,
    experiment_controller_reference=None,
    experiment_controller_required=False,
    experiment_authority_root=None,
):
    role = agent.get("role") or task.get("required_role")
    usage_stage = task.get("usage_stage") or _worker_usage_stage(
        role,
        task.get("task_kind"),
    )
    profile = _agent_runtime_profile(agent_pool, agent)
    requested_session = (
        task.get("requested_provider_session_id")
        or task.get("provider_session_id")
        or profile.get("resume_session_id")
    )
    resume_last = bool(
        task.get("provider_resume_mode") == "resume_last"
        or task.get("provider_resume_last")
        or profile.get("resume_last")
    )
    if requested_session and resume_last:
        raise ValueError(
            "provider session id and resume_last are mutually exclusive"
        )
    resume_mode = (
        "explicit"
        if requested_session
        else "resume_last"
        if resume_last
        else "new"
    )
    project_name = (
        task.get("project")
        or backlog.get("project")
        or (Path(project_root).name if project_root else "agentteam")
    )
    taskpack_id = (
        task.get("taskpack_id")
        or backlog.get("taskpack_id")
        or backlog.get("backlog_id")
        or Path(output_dir).name
    )
    project_identity = (
        str(Path(project_root).resolve())
        if project_root
        else str(
            task.get("provider_project_identity")
            or Path(output_dir).parent.resolve()
        )
    )
    context = {
        "project": project_name,
        "run_id": task.get("run_id") or backlog.get("run_id") or run_id,
        "pursue_id": task.get("pursue_id"),
        "round_index": task.get("round_index"),
        "taskpack_id": taskpack_id,
        "implementation_run_id": task.get("implementation_run_id"),
        "gate_epoch": task.get("gate_epoch"),
        "runtime_execution_session_id": runtime_execution_session_id,
        "requested_provider_session_id": requested_session,
        "provider_resume_mode": resume_mode,
        "provider_predecessor_invocation_id": task.get(
            "provider_predecessor_invocation_id"
        ),
        "provider_predecessor_turn_id": task.get(
            "provider_predecessor_turn_id"
        ),
        "provider_predecessor_usage_snapshot": deepcopy(
            task.get("provider_predecessor_usage_snapshot")
        ),
        "lifecycle_owner_token": lease_id,
        "agent_id": agent["agent_id"],
        "agent_role": role,
        "required_role": role,
        "usage_stage": usage_stage,
        "provider_usage_scope": task.get("provider_usage_scope"),
        "provider_session_lock_held": False,
        "provider_project_binding_valid": False,
        "provider_lineage_status": task.get("provider_lineage_status"),
        "previous_provider_session_id": task.get("previous_provider_session_id"),
        "previous_provider_turn_id": task.get("previous_provider_turn_id"),
        "previous_invocation_id": task.get("previous_invocation_id"),
        "experiment_sandbox_reference": task.get(
            "experiment_sandbox_reference"
        ),
        "experiment_sandbox_required": (
            task.get("experiment_sandbox_required") is True
        ),
        "experiment_authority_root": experiment_authority_root,
        "experiment_controller_reference": deepcopy(
            experiment_controller_reference
        ),
        "experiment_controller_required": (
            experiment_controller_required is True
        ),
        "model_invocation_authority_root": str(Path(output_dir)),
        "provider_project_identity": project_identity,
        "provider_project_lifecycle_root": str(Path(output_dir).parent),
        "provider_backend_account": task.get("provider_backend_account", "default"),
        "provider_session_store": task.get("provider_session_store", "default"),
    }
    if task.get("provider_session_state_root"):
        context["provider_session_state_root"] = task["provider_session_state_root"]
    if task.get("decision_id"):
        context["decision_id"] = task["decision_id"]
    return context


def _worker_usage_stage(role, task_kind):
    if task_kind == "decompose_backlog":
        return "planner_or_task_slicer"
    return WORKER_USAGE_STAGE_BY_ROLE.get(role, "implementation_worker")


def _load_experiment_runtime_context(output_dir):
    path = Path(output_dir) / "experiment-runtime-context.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("experiment runtime context is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("experiment runtime context is unreadable") from exc
    required = {
        "schema_version",
        "experiment_run_id",
        "protocol_sha256",
        "run_manifest_sha256",
        "authority_root",
        "mode",
        "repository_identity",
        "controller_reference",
        "controller_required",
        "independent_attempt_workspaces",
        "model_policy",
        "sandbox_configuration",
        "sandbox_configuration_sha256",
    }
    optional = {
        "usage_stage",
        "resource_envelope_binding",
        "resource_envelope_required",
        "resource_project_id",
        "resource_hierarchy_reference",
    }
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - required - optional
        or value["schema_version"] != "experiment_runtime_context.v1"
        or value["controller_required"] is not True
        or value["independent_attempt_workspaces"] is not True
    ):
        raise ValueError("experiment runtime context fields are invalid")
    resource_binding = value.get("resource_envelope_binding")
    if resource_binding is not None:
        from .resource_envelope import validate_resource_envelope_binding

        validate_resource_envelope_binding(resource_binding)
        if (
            value.get("resource_envelope_required") is not True
            or not isinstance(value.get("resource_project_id"), str)
            or not isinstance(
                value.get("resource_hierarchy_reference"),
                dict,
            )
        ):
            raise ValueError(
                "experiment resource runtime context fields are invalid"
            )
    controller_reference = validate_experiment_controller_reference(
        value["controller_reference"],
    )
    mode_authority = load_experiment_mode_authority(
        value["authority_root"]
    )
    if (
        mode_authority["controller_reference"]
        != controller_reference
        or value["sandbox_configuration_sha256"]
        != canonical_json_sha256(
            value["sandbox_configuration"]
        )
        or mode_authority["sandbox_configuration_sha256"]
        != value["sandbox_configuration_sha256"]
    ):
        raise ValueError(
            "experiment runtime controller differs from mode authority"
        )
    return value


def _experiment_reference_id(value):
    normalized = "".join(
        character
        if character.islower()
        and (character.isalnum() or character in "-_")
        else character.lower()
        if character.isalnum()
        else "-"
        for character in str(value)
    )
    normalized = normalized.strip("-_")
    return normalized[:128] or "provider-launch"


def _agent_runtime_profile(agent_pool, agent):
    profile = agent.get("runtime_profile")
    if isinstance(profile, dict):
        return profile
    role_profiles = agent_pool.get("role_runtime_profiles")
    if isinstance(role_profiles, dict):
        profile = role_profiles.get(agent.get("role"))
        if isinstance(profile, dict):
            return profile
    return {}


def reconcile_orphaned_invocation(
    authority_root,
    inflight,
    *,
    fence_assessor=None,
    service_stopper=None,
):
    matches = _matching_invocation_starts(authority_root, inflight)
    if not matches:
        return {
            "reconciliation_status": "no_invocation",
            "proof": "no_durable_start_for_lease",
        }
    if len(matches) != 1:
        return {
            "reconciliation_status": "open_ambiguous",
            "proof": "multiple_durable_starts_for_lease",
            "invocation_ids": [record["invocation_id"] for _, record in matches],
        }
    started_path, start = matches[0]
    terminal_path = started_path.with_name("terminal.json")
    terminal = _read_json_file_if_exists(terminal_path)
    if terminal is not None:
        return _terminal_reconciliation("terminal_available", start, terminal_path, terminal)

    assessor = fence_assessor or _assess_persisted_execution_group
    assessment = assessor(deepcopy(start))
    status = assessment.get("fence_status")
    if status == "live_pinned":
        pidfd = assessment.get("pidfd")
        if isinstance(pidfd, int):
            os.close(pidfd)
        return {
            "reconciliation_status": "live",
            "invocation_id": start["invocation_id"],
            "proof": assessment.get("proof"),
        }
    if status == "exact_service_stop_required":
        stopper = service_stopper or _stop_exact_transient_service
        if not stopper(deepcopy(start)):
            return {
                "reconciliation_status": "service_stop_required",
                "invocation_id": start["invocation_id"],
                "proof": assessment.get("proof"),
            }
        terminal = _read_json_file_if_exists(terminal_path)
        if terminal is not None:
            return _terminal_reconciliation(
                "terminal_available",
                start,
                terminal_path,
                terminal,
            )
        assessment = (
            assessor(deepcopy(start))
            if fence_assessor is not None
            else _assess_stopped_exact_service(start)
        )
        status = assessment.get("fence_status")
    if status != "death_proven":
        return {
            "reconciliation_status": "open_ambiguous",
            "invocation_id": start["invocation_id"],
            "proof": assessment.get("proof") or "execution_group_identity_ambiguous",
        }

    terminal = _read_json_file_if_exists(terminal_path)
    if terminal is not None:
        return _terminal_reconciliation("terminal_available", start, terminal_path, terminal)
    lifecycle = _attach_existing_lifecycle(started_path, start)
    old_owner = start["lifecycle_owner_token"]
    lifecycle.context["lifecycle_owner_token"] = (
        f"RECOVERY-{start['invocation_id']}"
    )
    revocation = lifecycle.revoke_writer(
        old_owner,
        revoked_by="recovery-controller",
        reason="worker_process_death_confirmed",
    )
    terminal = _read_json_file_if_exists(terminal_path)
    if terminal is not None:
        return _terminal_reconciliation("terminal_available", start, terminal_path, terminal)
    stdout = _read_text_if_exists(lifecycle.stdout_path)
    stderr = _read_text_if_exists(lifecycle.stderr_path)
    try:
        terminal = lifecycle.finalize(
            "recovered_orphan",
            stdout=stdout,
            stderr=stderr,
            terminal_writer="recovery_controller",
        )
    except ModelInvocationIntegrityError:
        terminal = _read_json_file_if_exists(terminal_path)
        if terminal is None:
            raise
    return {
        **_terminal_reconciliation("recovered", start, terminal_path, terminal),
        "proof": assessment.get("proof"),
        "writer_revocation": revocation,
        "writer_revocation_path": str(lifecycle.revoked_path),
    }


def _runtime_result_from_reconciliation(inflight, reconciliation):
    status = reconciliation.get("reconciliation_status")
    if status not in {"terminal_available", "recovered"}:
        return None
    terminal = _read_json_file_if_exists(reconciliation.get("terminal_path"))
    if terminal is None:
        return None
    terminal_status = terminal.get("terminal_status")
    result_status = (
        terminal_status
        if terminal_status in {"completed", "failed", "blocked", "cancelled", "timed_out"}
        else "timed_out"
    )
    changed_files = []
    if inflight.get("worktree_path"):
        changed_files = audit_worktree_diff(
            inflight["worktree_path"],
            [],
        ).get("actual_changed_files", [])
    invocation_dir = Path(reconciliation["terminal_path"]).parent
    result = {
        "result_status": result_status,
        "changed_files": changed_files,
        "output": {
            "adapter": "two_phase_scheduler_reconciliation",
            "invocation_reconciliation": reconciliation,
            "model_invocation": {
                "invocation_id": terminal.get("invocation_id"),
                "usage_event_id": terminal.get("usage_event_id"),
                "started_path": str(invocation_dir / "started.json"),
                "terminal_path": str(invocation_dir / "terminal.json"),
                "stdout_path": str(invocation_dir / "stdout.jsonl"),
                "stderr_path": str(invocation_dir / "stderr.log"),
                "replayed_from_terminal": True,
            },
            "model_invocation_usage": terminal,
        },
    }
    if terminal.get("usage_status") in {"reported", "partial"}:
        result["token_usage"] = {
            **{
                field: terminal.get(field, 0)
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                )
            },
            "usage_source": "model_invocation_terminal",
        }
    return result


def _matching_invocation_starts(authority_root, inflight):
    matches = []
    invocation_root = Path(authority_root) / "model_invocations"
    if not invocation_root.exists():
        return matches
    for path in sorted(invocation_root.glob("*/started.json")):
        record = _read_json_file_if_exists(path)
        if record is None:
            continue
        if record.get("attempt_id") != inflight.get("attempt_id"):
            continue
        if record.get("lifecycle_owner_token") != inflight.get("lease_id"):
            continue
        if record.get("agent_id") not in {None, inflight.get("agent_id")}:
            continue
        matches.append((path, record))
    return matches


def _attach_existing_lifecycle(started_path, start):
    lifecycle = object.__new__(InvocationLifecycle)
    lifecycle.authority_root = Path(started_path).parents[2]
    lifecycle.invocation_id = start["invocation_id"]
    lifecycle.context = dict(start)
    lifecycle.started_at = start["started_at"]
    lifecycle.invocation_dir = Path(started_path).parent
    lifecycle.started_path = Path(started_path)
    lifecycle.revoked_path = lifecycle.invocation_dir / "revoked.json"
    lifecycle.terminal_path = lifecycle.invocation_dir / "terminal.json"
    lifecycle.terminal_lock_path = lifecycle.invocation_dir / "terminal.lock"
    lifecycle.stdout_path = lifecycle.invocation_dir / "stdout.jsonl"
    lifecycle.stderr_path = lifecycle.invocation_dir / "stderr.log"
    return lifecycle


def _terminal_reconciliation(status, start, terminal_path, terminal):
    return {
        "reconciliation_status": status,
        "invocation_id": start["invocation_id"],
        "usage_event_id": terminal.get("usage_event_id"),
        "terminal_status": terminal.get("terminal_status"),
        "terminal_path": str(terminal_path),
    }


def _assess_persisted_execution_group(start):
    current_boot_id = _read_host_boot_id()
    if current_boot_id is None:
        return {
            "fence_status": "open_ambiguous",
            "proof": "host_boot_identity_unavailable",
            "signal_allowed": False,
        }
    if current_boot_id != start.get("host_boot_id"):
        return assess_execution_group_fence(
            start,
            current_boot_id=current_boot_id,
            current_user_service=None,
            current_manager_identity=None,
            current_transient_service=None,
        )
    user_service = _systemd_show(
        ["systemctl", "show", f"user@{os.getuid()}.service"],
        ("InvocationID", "ControlGroup", "KillMode", "ActiveState"),
    )
    manager = _systemd_show(
        ["systemctl", "--user", "show"],
        ("ManagerTimestampMonotonic",),
    )
    manager_value = (
        manager.get("ManagerTimestampMonotonic")
        if isinstance(manager, dict)
        else None
    )
    manager_identity = (
        f"manager-monotonic:{manager_value}" if manager_value else None
    )
    transient = _systemd_show(
        ["systemctl", "--user", "show", start.get("systemd_transient_unit", "")],
        ("InvocationID", "ControlGroup", "KillMode", "ActiveState"),
    )
    return assess_execution_group_fence(
        start,
        current_boot_id=current_boot_id,
        current_user_service=user_service,
        current_manager_identity=manager_identity,
        current_transient_service=transient,
    )


def _stop_exact_transient_service(start):
    unit = start.get("systemd_transient_unit")
    if (
        not isinstance(unit, str)
        or not unit.startswith("agentteam-inv-")
        or not unit.endswith(".service")
        or "/" in unit
    ):
        return False
    current = _systemd_show(
        ["systemctl", "--user", "show", unit],
        ("InvocationID", "ControlGroup", "KillMode", "ActiveState"),
    )
    if (
        not isinstance(current, dict)
        or current.get("InvocationID")
        != start.get("systemd_transient_invocation_id")
        or current.get("ControlGroup")
        != start.get("systemd_transient_control_group")
        or current.get("KillMode")
        != start.get("systemd_transient_kill_mode")
    ):
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _assess_stopped_exact_service(start):
    populated = _persisted_cgroup_populated(
        start.get("systemd_transient_control_group")
    )
    if populated is False:
        return {
            "fence_status": "death_proven",
            "proof": "exact_transient_service_stopped_and_cgroup_empty",
            "signal_allowed": False,
        }
    if populated is True:
        return {
            "fence_status": "exact_service_stop_required",
            "proof": "exact_transient_cgroup_still_populated",
            "signal_allowed": False,
        }
    return {
        "fence_status": "open_ambiguous",
        "proof": "transient_cgroup_population_unknown_after_stop",
        "signal_allowed": False,
    }


def _persisted_cgroup_populated(control_group):
    if not isinstance(control_group, str) or not control_group.startswith("/"):
        return None
    cgroup_root = Path("/sys/fs/cgroup").resolve()
    events_path = (cgroup_root / control_group.lstrip("/") / "cgroup.events").resolve()
    try:
        events_path.relative_to(cgroup_root)
        values = {
            key: value
            for key, value in (
                line.split(None, 1)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if len(line.split(None, 1)) == 2
            )
        }
    except (OSError, ValueError):
        return None
    if values.get("populated") == "0":
        return False
    if values.get("populated") == "1":
        return True
    return None


def _systemd_show(command, properties):
    full_command = list(command)
    for prop in properties:
        full_command.append(f"--property={prop}")
    try:
        completed = subprocess.run(
            full_command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    values = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _read_host_boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None
    return value or None


def _read_json_file_if_exists(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write_dispatch_authority(step_dir, message):
    message_id = message["message_id"]
    file_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    authority_path = (
        Path(step_dir)
        / "state"
        / "mailbox_dispatch_authority"
        / f"{file_id}.json"
    )
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "mailbox_dispatch_authority.v1",
        "message_id": message_id,
        "message_sha256": hashlib.sha256(
            json.dumps(
                message,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary_path = authority_path.with_name(
        f".{authority_path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary_path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short mailbox dispatch authority write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(
                temporary_path,
                authority_path,
                follow_symlinks=False,
            )
        except FileExistsError:
            _verify_dispatch_authority_bytes(
                authority_path,
                encoded,
            )
        else:
            directory_fd = os.open(
                authority_path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _verify_dispatch_authority_bytes(authority_path, expected):
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    read_flags |= getattr(os, "O_NOFOLLOW", 0)
    read_flags |= getattr(os, "O_NONBLOCK", 0)
    existing_fd = os.open(authority_path, read_flags)
    try:
        metadata = os.fstat(existing_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != len(expected)
        ):
            raise RuntimeError(
                "mailbox dispatch authority conflicts with retry"
            )
        chunks = []
        remaining = len(expected)
        while remaining:
            chunk = os.read(existing_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if b"".join(chunks) != expected:
            raise RuntimeError(
                "mailbox dispatch authority conflicts with retry"
            )
    finally:
        os.close(existing_fd)


def _read_text_if_exists(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _decomposition_validation_payload(result):
    payload = {}
    if "decomposition_status" in result:
        payload["decomposition_status"] = result["decomposition_status"]
    if "decomposition_error" in result:
        payload["decomposition_error"] = result["decomposition_error"]
    return payload


def _runtime_evidence_summary(task, runtime_result):
    output = runtime_result.get("output", {}) if isinstance(runtime_result.get("output"), dict) else {}
    raw = output.get("evidence_summary")
    if raw is None and any(
        key in output
        for key in (
            "evidence_level",
            "evidence_status",
            "trace_carrier",
            "missing_evidence",
        )
    ):
        raw = output
    if raw is None:
        raw = {}
    risk_target = task.get("risk_target") or "L1"
    if risk_target in {"L2", "L3"} and not raw:
        raw = {
            "evidence_level": risk_target,
            "missing_evidence": ["evidence_summary"],
        }
    try:
        summary = normalize_evidence_summary(raw, default_level=risk_target)
    except ValueError as exc:
        summary = {
            "evidence_level": risk_target,
            "evidence_status": "incomplete",
            "trace_carrier": [],
            "missing_evidence": [f"invalid_evidence_summary: {exc}"],
        }
    return {
        "evidence_summary": summary,
        "evidence_level": summary["evidence_level"],
        "evidence_status": summary["evidence_status"],
        "trace_carrier": summary["trace_carrier"],
        "missing_evidence": summary["missing_evidence"],
    }


def _runtime_verification_additions(runtime_result):
    output = (
        runtime_result.get("output", {})
        if isinstance(runtime_result.get("output"), dict)
        else {}
    )
    return output.get("verification_additions", [])


def _integration_blocked_by_evidence(result, patch_path):
    if not patch_path:
        return False
    if result.get("evidence_level") not in {"L2", "L3"}:
        return False
    if result.get("evidence_status") != "complete":
        return True
    return bool(result.get("missing_evidence"))


def _decomposition_wave_from_task_id(task_id):
    try:
        return int(str(task_id).rsplit("-", 1)[1])
    except (TypeError, ValueError):
        return 1


def _is_terminal_backlog_status(task):
    return task.get("backlog_status") in {"done", "blocked"}


def _operator_report_from_state(state):
    report_artifact = state.get("operator_report_artifact")
    if isinstance(report_artifact, dict):
        return load_operator_report(report_artifact)
    task_reports = []
    for step in state.get("steps", []):
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        task_reports.append(_operator_task_report(step, result))
    blocked_task_ids = {
        item.get("task_id")
        for item in state.get("backlog", {}).get("items", [])
        if isinstance(item, dict)
        and item.get("backlog_status") == "blocked"
        and item.get("task_id")
    }
    review_task_ids = {
        report.get("task_id")
        for report in task_reports
        if _operator_task_needs_review(report) and report.get("task_id")
    }
    anonymous_review_count = sum(
        1
        for report in task_reports
        if _operator_task_needs_review(report) and not report.get("task_id")
    )
    token_usages = [report.get("token_usage") for report in task_reports]
    return {
        "report_schema_version": "operator_run_report.v1",
        "task_count": len(task_reports),
        "blocked_count": (
            len(blocked_task_ids | review_task_ids)
            + anonymous_review_count
        ),
        "blocked_task_ids": sorted(blocked_task_ids | review_task_ids),
        "token_usage": aggregate_token_usage(token_usages, expected_count=len(task_reports)),
        "task_reports": task_reports,
    }


def _operator_task_needs_review(report):
    status = str(report.get("status") or "").lower()
    integration = str(report.get("integration") or "").lower()
    return (
        "blocked" in status
        or "rejected" in status
        or "failed" in status
        or "timed_out" in status
        or "timed out" in status
        or integration.startswith("failed")
    )


def _integration_baseline_event_fields(baseline):
    if not isinstance(baseline, dict):
        return {}
    return {
        "integration_base_ref": baseline.get("integration_baseline_branch"),
        "integration_base_sha": baseline.get("integration_baseline_head_sha"),
        "integration_baseline_branch": baseline.get("integration_baseline_branch"),
        "integration_baseline_worktree_path": baseline.get(
            "integration_baseline_worktree_path"
        ),
    }


def _integration_baseline_inflight_fields(baseline):
    if not isinstance(baseline, dict):
        return {
            "integration_base_ref": None,
            "integration_base_sha": None,
            "integration_baseline_branch": None,
            "integration_baseline_worktree_path": None,
        }
    return {
        "integration_base_ref": baseline.get("integration_baseline_branch"),
        "integration_base_sha": baseline.get("integration_baseline_head_sha"),
        "integration_baseline_branch": baseline.get("integration_baseline_branch"),
        "integration_baseline_worktree_path": baseline.get(
            "integration_baseline_worktree_path"
        ),
    }


def _integration_commit_from_baseline_result(result):
    return {
        "integration_commit_status": result.get("integration_baseline_commit_status"),
        "integration_commit_sha": result.get("integration_baseline_commit_sha"),
        "integration_commit_message": result.get(
            "integration_baseline_commit_message"
        ),
        "integration_commit_reason": result.get("integration_baseline_commit_reason"),
        "integration_commit_stdout": result.get(
            "integration_baseline_commit_stdout",
            "",
        ),
        "integration_commit_stderr": result.get(
            "integration_baseline_commit_stderr",
            "",
        ),
    }


def _operator_task_report(step, result):
    output = result.get("runtime_output") if isinstance(result.get("runtime_output"), dict) else {}
    operator_summary = (
        output.get("operator_summary")
        if isinstance(output.get("operator_summary"), dict)
        else {}
    )
    return {
        "task_id": result.get("task_id") or step.get("task_id") or "unknown",
        "attempt_id": result.get("attempt_id"),
        "status": _operator_task_status(result),
        "what_changed": _operator_what_changed(output, operator_summary),
        "changed_files": _operator_changed_files(result),
        "verification": _operator_verification(output, operator_summary),
        "measured_result": _operator_measured_result(output, operator_summary),
        "integration": _operator_integration_summary(result),
        "evidence_level": result.get("evidence_level"),
        "evidence_status": result.get("evidence_status"),
        "trace_carrier": result.get("trace_carrier", []),
        "missing_evidence": result.get("missing_evidence", []),
        "merge_recommendation": _operator_merge_recommendation(result, operator_summary),
        "next_steps": _operator_next_steps(result, operator_summary),
        "token_usage": token_usage_from_result(result),
        "agentteam_target_review_required": _agentteam_target_review_required(output, operator_summary),
    }


def _operator_task_status(result):
    validation = result.get("validation_status")
    integration = result.get("integration_verification_status")
    if integration == "failed":
        return "implementation completed, integration blocked"
    if validation == "accepted":
        return "implementation completed"
    if validation == "rejected":
        return "implementation rejected"
    return result.get("failure_category") or validation or "unknown"


def _operator_what_changed(output, operator_summary):
    for key in ["what_changed", "summary"]:
        values = _coerce_text_list(operator_summary.get(key))
        if values:
            return values
    values = _coerce_text_list(output.get("summary"))
    if values:
        return values
    behavior_change = operator_summary.get("behavior_change")
    if behavior_change:
        return [str(behavior_change)]
    return ["Worker did not provide a natural-language change summary."]


def _operator_changed_files(result):
    changed_files = _coerce_text_list(result.get("changed_files"))
    if changed_files:
        return changed_files
    diff_audit = result.get("diff_audit") if isinstance(result.get("diff_audit"), dict) else {}
    return _coerce_text_list(
        diff_audit.get("actual_changed_files") or diff_audit.get("declared_changed_files")
    )


def _operator_verification(output, operator_summary):
    explicit = _coerce_text_list(operator_summary.get("verification_summary"))
    if explicit:
        return explicit
    verification = output.get("verification")
    if not isinstance(verification, dict):
        return []
    lines = []
    for name, item in verification.items():
        if isinstance(item, dict):
            status = item.get("status") or item.get("result") or "unknown"
        else:
            status = str(item)
        lines.append(f"{name}: {status}")
    return lines


def _operator_measured_result(output, operator_summary):
    for key in ["measured_result", "measured_results", "metric_delta"]:
        values = _coerce_text_list(operator_summary.get(key))
        if values:
            return values
    return _coerce_text_list(output.get("measured_result") or output.get("metric_delta"))


def _operator_integration_summary(result):
    status = result.get("integration_verification_status")
    if status == "failed":
        additions_status = result.get("integration_verification_additions_status")
        if additions_status in {"failed", "rejected"}:
            labels = [
                item.get("label")
                for item in result.get("integration_verification_additions", [])
                if isinstance(item, dict)
                and item.get("verification_addition_status") == additions_status
            ]
            label_text = ", ".join(labels) if labels else "worker verification addition"
            return f"failed: {additions_status} verification addition {label_text}"
        failure = _first_failure_line(result.get("integration_verification_stderr", ""))
        return f"failed: {failure}" if failure else "failed"
    if status in {None, "not_requested"}:
        return "not requested"
    if status == "passed":
        if result.get("integration_verification_additions_status") == "passed":
            count = len(result.get("integration_verification_additions", []))
            return f"passed with {count} worker verification addition(s)"
        return "passed"
    return str(status)


def _operator_merge_recommendation(result, operator_summary):
    explicit = operator_summary.get("merge_recommendation")
    if explicit:
        return str(explicit)
    if result.get("integration_verification_status") == "failed":
        return "Do not merge until integration passes."
    if result.get("validation_status") == "accepted":
        return "Review accepted patch before merging."
    return "Do not merge until the task is accepted."


def _operator_next_steps(result, operator_summary):
    explicit = _coerce_text_list(operator_summary.get("next_steps"))
    if explicit:
        return explicit
    if result.get("integration_verification_status") == "failed":
        return ["Review the failing integration test and update the patch or generated artifacts."]
    return []


def _agentteam_target_review_required(output, operator_summary):
    deliverables = operator_summary.get("deliverables") or output.get("deliverables")
    if isinstance(deliverables, dict):
        return bool(deliverables.get("agentteam_target_review_gate"))
    if isinstance(deliverables, list):
        for item in deliverables:
            if isinstance(item, dict):
                name = item.get("deliverable") or item.get("name") or item.get("id")
                if name == "agentteam_target_review_gate":
                    return True
            elif str(item) == "agentteam_target_review_gate":
                return True
    return False


def _acceptance_evidence_refs(result):
    refs = [
        f"attempt:{result.get('attempt_id')}:validation:{result.get('validation_status')}"
    ]
    evidence_status = result.get("evidence_status")
    if evidence_status:
        refs.append(f"attempt:{result.get('attempt_id')}:evidence:{evidence_status}")
    refs.extend(
        item
        for item in result.get("trace_carrier", [])
        if isinstance(item, str) and item
    )
    return refs


def _first_failure_line(text):
    for line in reversed(str(text or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("FAILED")
            or stripped.startswith("FAIL:")
            or stripped.startswith("ERROR:")
            or "ModuleNotFoundError" in stripped
        ):
            return stripped
    return None


def _coerce_text_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


def _manual_gate_payload(inflight, runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    if not isinstance(output, dict):
        output = {}
    manual_gate = output.get("manual_gate")
    if not isinstance(manual_gate, dict):
        manual_gate = {}
    question = _first_non_empty_string(
        manual_gate.get("question"),
        output.get("question"),
        "Worker requested operator guidance before continuing.",
    )
    options = manual_gate.get("options", [])
    if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
        options = []
    return {
        "task_id": inflight["task_id"],
        "attempt_id": inflight["attempt_id"],
        "lease_id": inflight["lease_id"],
        "question_id": f"Q-{inflight['attempt_id']}",
        "gate_status": "waiting",
        "question": question,
        "options": options,
        "reason": _first_non_empty_string(
            manual_gate.get("reason"),
            output.get("reason"),
            None,
        ),
        "guidance_scope": _first_non_empty_string(
            manual_gate.get("guidance_scope"),
            "next_attempt",
        ),
    }


def _runtime_result_has_manual_gate(runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    return isinstance(output, dict) and isinstance(output.get("manual_gate"), dict)


def _permission_request_payload(inflight, runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    if not isinstance(output, dict):
        return None
    request = output.get("permission_request")
    if not isinstance(request, dict):
        return None
    requested_capability = _first_non_empty_string(
        request.get("requested_capability"),
        request.get("capability"),
        "runtime_permission",
    )
    reason = _first_non_empty_string(
        request.get("reason"),
        output.get("reason"),
        "Worker requested an operator-approved runtime capability before continuing.",
    )
    payload = {
        "task_id": inflight["task_id"],
        "attempt_id": inflight["attempt_id"],
        "lease_id": inflight["lease_id"],
        "request_id": f"PERM-{inflight['attempt_id']}",
        "request_status": "waiting",
        "request_type": _first_non_empty_string(
            request.get("request_type"),
            "runtime_permission",
        ),
        "requested_capability": requested_capability,
        "reason": reason,
        "scope": _first_non_empty_string(request.get("scope"), "next_attempt"),
    }
    for key in ["command", "sandbox", "exit_code"]:
        if key in request:
            payload[key] = request[key]
    return payload


def _operator_guidance_fields(task):
    guidance = task.get("operator_guidance")
    if not guidance:
        return {}
    if not isinstance(guidance, list):
        return {}
    safe_guidance = [
        {
            "question_id": item.get("question_id"),
            "answer": item.get("answer"),
            "operator": item.get("operator"),
        }
        for item in guidance
        if isinstance(item, dict)
    ]
    return {"operator_guidance": safe_guidance} if safe_guidance else {}


def _evidence_policy_fields(task):
    evidence_level = task.get("risk_target")
    if evidence_level not in {"L0", "L1", "L2", "L3"}:
        evidence_level = "L1"
    return {
        "evidence_policy": {
            "evidence_level": evidence_level,
            "required_result_key": "evidence_summary",
            "required_fields": [
                "evidence_level",
                "evidence_status",
                "trace_carrier",
                "missing_evidence",
            ],
            "integration_requires_complete_evidence": evidence_level in {"L2", "L3"},
        }
    }


def _permission_grant_fields(task):
    grants = task.get("permission_grants")
    if not grants:
        return {}
    if not isinstance(grants, list):
        return {}
    safe_grants = [
        {
            "request_id": item.get("request_id"),
            "requested_capability": item.get("requested_capability"),
            "operator": item.get("operator"),
            "reason": item.get("reason"),
        }
        for item in grants
        if isinstance(item, dict)
    ]
    return {"permission_grants": safe_grants} if safe_grants else {}


def _first_non_empty_string(*values):
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _runtime_result_from_outbox(outbox_path, source_message_id):
    for record in _read_jsonl_if_exists(outbox_path):
        if record.get("message_type") != "runtime_result":
            continue
        payload = record.get("payload", {})
        if payload.get("source_message_id") != source_message_id:
            continue
        return {
            "result_status": payload.get("result_status", "failed"),
            "changed_files": payload.get("changed_files", []),
            "output": payload.get("output", {}),
            "usage": payload.get("usage"),
            "token_usage": payload.get("token_usage"),
            "model_invocation_context": payload.get("model_invocation_context"),
            "model_invocation": payload.get("model_invocation"),
        }
    return None


def _read_jsonl_if_exists(path):
    path = Path(path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _timestamp_after(timestamp, seconds):
    return _format_utc_timestamp(
        _parse_utc_timestamp(timestamp) + timedelta(seconds=seconds)
    )


def _parse_utc_timestamp(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)


def _format_utc_timestamp(timestamp):
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

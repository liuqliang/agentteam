from importlib import import_module


_EXPORTS = {
    "CodexRuntimeAdapter": (".m0_runtime", "CodexRuntimeAdapter"),
    "FakeRuntimeAdapter": (".m0_runtime", "FakeRuntimeAdapter"),
    "FileMailboxExternalRuntimeAdapter": (
        ".mailbox_worker",
        "FileMailboxExternalRuntimeAdapter",
    ),
    "FileMailboxRuntimeAdapter": (".mailbox_worker", "FileMailboxRuntimeAdapter"),
    "FileMailboxSubprocessRuntimeAdapter": (
        ".mailbox_worker",
        "FileMailboxSubprocessRuntimeAdapter",
    ),
    "FileMailboxWorker": (".mailbox_worker", "FileMailboxWorker"),
    "FileMailboxWorkerProcessSupervisor": (
        ".mailbox_worker",
        "FileMailboxWorkerProcessSupervisor",
    ),
    "FileMailboxWorkerPoolSupervisor": (
        ".worker_pool",
        "FileMailboxWorkerPoolSupervisor",
    ),
    "FeishuWebhookNotifier": (".notifications", "FeishuWebhookNotifier"),
    "FileScheduler": (".m0_runtime", "FileScheduler"),
    "FileSchedulerDaemon": (".daemon", "FileSchedulerDaemon"),
    "merge_verified_integration_batch": (
        ".integration_batch",
        "merge_verified_integration_batch",
    ),
    "ShellRuntimeAdapter": (".m0_runtime", "ShellRuntimeAdapter"),
    "TaskpackValidationError": (".taskpack", "TaskpackValidationError"),
    "TwoPhaseFileScheduler": (".two_phase_scheduler", "TwoPhaseFileScheduler"),
    "answer_manual_gate": (".m0_runtime", "answer_manual_gate"),
    "audit_worktree_diff": (".m0_runtime", "audit_worktree_diff"),
    "build_planner_context": (".planner_context", "build_planner_context"),
    "build_repo_context": (".repo_map", "build_repo_context"),
    "build_repository_map": (".repo_map", "build_repository_map"),
    "build_runtime_observability": (".observability", "build_runtime_observability"),
    "build_project_stats": (".projection_db", "build_project_stats"),
    "build_taskpack_runtime_args": (".taskpack", "build_taskpack_runtime_args"),
    "build_feishu_notification_sink_from_env": (
        ".notifications",
        "build_feishu_notification_sink_from_env",
    ),
    "check_project_projection_db": (
        ".projection_db",
        "check_project_projection_db",
    ),
    "classify_attempt_outcome": (".m0_runtime", "classify_attempt_outcome"),
    "draft_taskpack_files": (".taskpack", "draft_taskpack_files"),
    "draft_taskpack_from_goal": (".taskpack_author", "draft_taskpack_from_goal"),
    "freeze_taskpack": (".taskpack", "freeze_taskpack"),
    "feishu_custom_bot_sign": (".notifications", "feishu_custom_bot_sign"),
    "load_taskpack": (".taskpack", "load_taskpack"),
    "list_permission_requests": (".m0_runtime", "list_permission_requests"),
    "normalize_evidence_summary": (".task_proposal", "normalize_evidence_summary"),
    "normalize_task_proposal": (".task_proposal", "normalize_task_proposal"),
    "read_integration_batches": (".integration_batch", "read_integration_batches"),
    "read_scheduler_state_index": (".m0_runtime", "read_scheduler_state_index"),
    "read_integration_queue": (".integration_queue", "read_integration_queue"),
    "read_projected_taskpacks": (
        ".projection_db",
        "read_projected_taskpacks",
    ),
    "read_projected_run_events": (
        ".projection_db",
        "read_projected_run_events",
    ),
    "read_projected_run_metadata": (
        ".projection_db",
        "read_projected_run_metadata",
    ),
    "read_projected_artifact_summary": (
        ".projection_db",
        "read_projected_artifact_summary",
    ),
    "read_projected_artifact_retention_plan": (
        ".projection_db",
        "read_projected_artifact_retention_plan",
    ),
    "rebuild_project_projection_db": (
        ".projection_db",
        "rebuild_project_projection_db",
    ),
    "resolve_permission_request": (".m0_runtime", "resolve_permission_request"),
    "replay_event_records": (".m0_runtime", "replay_event_records"),
    "replay_events": (".m0_runtime", "replay_events"),
    "run_file_daemon": (".daemon", "run_file_daemon"),
    "run_scheduler_loop": (".m0_runtime", "run_scheduler_loop"),
    "run_simulation": (".m0_runtime", "run_simulation"),
    "run_two_phase_scheduler_loop": (
        ".two_phase_scheduler",
        "run_two_phase_scheduler_loop",
    ),
    "validate_taskpack": (".taskpack", "validate_taskpack"),
    "verify_integration_batch": (".integration_batch", "verify_integration_batch"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

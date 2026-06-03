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
    "FileScheduler": (".m0_runtime", "FileScheduler"),
    "FileSchedulerDaemon": (".daemon", "FileSchedulerDaemon"),
    "merge_verified_integration_batch": (
        ".integration_batch",
        "merge_verified_integration_batch",
    ),
    "ShellRuntimeAdapter": (".m0_runtime", "ShellRuntimeAdapter"),
    "TwoPhaseFileScheduler": (".two_phase_scheduler", "TwoPhaseFileScheduler"),
    "audit_worktree_diff": (".m0_runtime", "audit_worktree_diff"),
    "build_planner_context": (".planner_context", "build_planner_context"),
    "classify_attempt_outcome": (".m0_runtime", "classify_attempt_outcome"),
    "normalize_task_proposal": (".task_proposal", "normalize_task_proposal"),
    "read_integration_batches": (".integration_batch", "read_integration_batches"),
    "read_scheduler_state_index": (".m0_runtime", "read_scheduler_state_index"),
    "read_integration_queue": (".integration_queue", "read_integration_queue"),
    "replay_events": (".m0_runtime", "replay_events"),
    "run_file_daemon": (".daemon", "run_file_daemon"),
    "run_scheduler_loop": (".m0_runtime", "run_scheduler_loop"),
    "run_simulation": (".m0_runtime", "run_simulation"),
    "run_two_phase_scheduler_loop": (
        ".two_phase_scheduler",
        "run_two_phase_scheduler_loop",
    ),
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

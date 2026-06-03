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
    "ShellRuntimeAdapter": (".m0_runtime", "ShellRuntimeAdapter"),
    "TwoPhaseFileScheduler": (".two_phase_scheduler", "TwoPhaseFileScheduler"),
    "audit_worktree_diff": (".m0_runtime", "audit_worktree_diff"),
    "classify_attempt_outcome": (".m0_runtime", "classify_attempt_outcome"),
    "read_scheduler_state_index": (".m0_runtime", "read_scheduler_state_index"),
    "replay_events": (".m0_runtime", "replay_events"),
    "run_file_daemon": (".daemon", "run_file_daemon"),
    "run_scheduler_loop": (".m0_runtime", "run_scheduler_loop"),
    "run_simulation": (".m0_runtime", "run_simulation"),
    "run_two_phase_scheduler_loop": (
        ".two_phase_scheduler",
        "run_two_phase_scheduler_loop",
    ),
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

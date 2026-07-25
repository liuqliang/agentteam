import argparse
import ctypes
import errno
import fcntl
import getpass
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .diagnostic_chat import (
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    build_runtime_diagnostic_context,
    render_runtime_diagnostic_context,
    run_runtime_diagnostic_chat,
)
from .artifact_repo import snapshot_run_artifacts_safe
from .m0_runtime import (
    answer_manual_gate,
    list_permission_requests,
    replay_event_records,
    replay_events,
    resolve_permission_request,
)
from .operator_control import (
    build_run_liveness_summary,
    cleanup_stale_runs,
    read_event_records_since,
    stop_run,
)
from .operator_report import (
    build_run_completion_report,
    concise_report_lines,
    find_pursue_recap_for_run,
    render_run_completion_report,
)
from .completion_summary import compact_text_items
from .goal_memory import (
    build_goal_memory,
    goal_memory_path,
    render_goal_memory_prompt_context,
    write_goal_memory,
)
from .follow_up_queue import build_follow_up_queue_summary, render_follow_up_queue_text
from .repo_grounding import build_repo_grounding, render_repo_grounding_text
from .semantic_feedback import (
    list_semantic_feedback_proposals,
    render_semantic_feedback_text,
    write_semantic_feedback_proposal,
)
from .profile import (
    AgentTeamProfileError,
    build_project_profile,
    default_project_key,
    default_work_root,
    effective_project_verification_profile,
    load_project_profile,
    profile_path_for_project,
    write_project_profile,
)
from .projection_db import (
    build_project_stats,
    check_project_projection_db,
    read_projected_artifact_retention_plan,
    read_projected_artifact_summary,
    read_projected_run_events,
    read_projected_run_metadata,
    read_projected_taskpacks,
    rebuild_project_projection_db,
)
from .release_manager import (
    activate_release,
    active_release_identity,
    adopt_legacy_implementation_run,
    install_release_from_git,
    install_release_from_checkout,
    publish_implementation_run,
    record_active_release_for_run,
    prune_global_releases,
    prune_releases,
    update_status,
    selected_release_identity,
    validate_acceptance_run_identity,
    validate_run_binding,
)
from .notifications import (
    FeishuWebhookNotifier,
    build_feishu_notification_sink_from_env,
    diagnose_feishu_webhook_delivery,
)
from .taskpack import (
    REPO_MAP_HANDOFF_PATH,
    build_taskpack_runtime_args,
    draft_taskpack_files,
    freeze_taskpack,
    load_taskpack,
    materialize_semantic_taskpack,
    materialize_taskpack_blueprint,
    reuse_repo_map_handoff_in_taskpack,
    validate_taskpack,
)
from .taskpack_author import draft_taskpack_from_goal
from .token_usage import format_token_usage, token_usage_from_state
from .two_phase_scheduler import TwoPhaseFileScheduler


AUTHOR_RUNTIME_CHOICES = ["fake", "codex", "deterministic"]
_PHASE1_REPORT_REVIEW_PATHS = [
    "experiments/native_agentteam_runtime/implementation_artifacts/reports/"
    "phase1-model-invocation-usage.md",
    "experiments/native_agentteam_runtime/implementation_artifacts/"
    "native_runtime_roadmap.md",
]


class AgentTeamCliError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentTeamCliError(message)


_LAUNCHER_SELECTION_ENV = "AGENTTEAM_LAUNCHER_SELECTION"


def _launcher_runtime_selection():
    raw = os.environ.get(_LAUNCHER_SELECTION_ENV)
    if not raw:
        return None
    try:
        selection = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentTeamCliError("launcher runtime selection is not valid JSON") from exc
    if not isinstance(selection, dict) or (
        selection.get("selection_version") != "launcher_runtime_selection.v1"
    ):
        raise AgentTeamCliError("launcher runtime selection has an invalid version")
    return selection


def _validate_launcher_runtime_selection(args):
    selection = _launcher_runtime_selection()
    if not selection:
        return None
    release = selection.get("release")
    if not isinstance(release, dict):
        raise AgentTeamCliError("launcher runtime selection is missing release identity")
    loaded_runtime_root = Path(__file__).resolve().parents[1]
    selected_runtime_root = Path(str(release.get("runtime_root") or "")).expanduser().resolve()
    if loaded_runtime_root != selected_runtime_root:
        raise AgentTeamCliError(
            "imported runtime root does not match launcher-selected release",
            imported_runtime_root=str(loaded_runtime_root),
            selected_runtime_root=str(selected_runtime_root),
        )
    run_dir = selection.get("run_dir")
    identity_digest = selection.get("identity_sha256")
    if run_dir and identity_digest:
        pair = validate_run_binding(
            run_dir,
            expected_project_key=selection.get("project_key"),
            expected_release=selection.get("expected_release") or {},
            expected_identity_sha256=identity_digest,
        )
        binding = pair["binding"]
        for key in (
            "release_id",
            "release_root",
            "runtime_root",
            "release_manifest_sha256",
            "source_commit",
            "git_object_format",
        ):
            if binding.get(key) != release.get(key):
                raise AgentTeamCliError(
                    f"launcher-selected release {key} does not match the immutable run binding"
                )
    return selection


_HELP_COMMANDS = [
    {
        "name": "init",
        "summary": "Create or update the project AgentTeam profile.",
        "examples": ["agentteam init --interactive"],
    },
    {
        "name": "start",
        "summary": "Author a taskpack from a goal, freeze it, and run it.",
        "examples": [
            "agentteam start",
            "agentteam start --goal \"optimize this repo\"",
            "agentteam start --goal \"optimize this repo\" --json",
        ],
    },
    {
        "name": "next",
        "summary": "Create a follow-up taskpack from a previous run report.",
        "examples": [
            "agentteam next --from-taskpack <id> --goal \"continue optimizing\"",
            "agentteam next --goal \"continue from the latest run\" --json",
        ],
    },
    {
        "name": "queue",
        "summary": "Inspect read-only follow-up queue suggestions for a completed run.",
        "examples": [
            "agentteam queue show --taskpack <id>",
            "agentteam queue next --taskpack <id>",
            "agentteam queue show --run-dir <run> --json",
        ],
        "notes": [
            "Does not draft taskpacks, start workers, merge code, or mutate run artifacts.",
            "Use the printed agentteam next command when you decide to continue.",
        ],
    },
    {
        "name": "feedback",
        "summary": "Create or list semantic feedback proposals for design-authority review.",
        "examples": [
            "agentteam feedback propose --taskpack <id> --proposal-id design-gap-1 --target-artifact design/system.md --summary \"...\" --rationale \"...\"",
            "agentteam feedback list --json",
        ],
        "notes": [
            "Proposal artifacts are written under <work_root>/semantic_feedback/.",
            "This command does not edit design authority documents.",
        ],
    },
    {
        "name": "pursue",
        "summary": "Run a bounded long-goal loop across start and follow-up taskpacks.",
        "examples": [
            "agentteam pursue --goal \"持续优化准确率和延迟\" --max-rounds 3",
            "agentteam pursue --goal \"持续优化准确率和延迟\" --max-rounds 3 --json",
        ],
        "notes": [
            "Stops at manual gates, permission requests, blockers, review gates, or budget limits.",
            "Does not merge source changes or bypass operator review.",
        ],
    },
    {
        "name": "status",
        "summary": "Show the latest run state, including liveness and workers.",
        "examples": ["agentteam status --project-root <repo>"],
    },
    {
        "name": "stats",
        "summary": "Show compact project-level run, artifact, evidence, and token statistics.",
        "examples": [
            "agentteam stats --project-root <repo>",
            "agentteam stats --project-root <repo> --json",
        ],
        "notes": [
            "Uses a fresh agentteam.db projection when available and falls back to file scanning otherwise.",
        ],
    },
    {
        "name": "grounding",
        "summary": "Read the target repository and summarize languages, project tools, tests, and candidate verification commands.",
        "examples": [
            "agentteam grounding --project-root <repo>",
            "agentteam grounding --project-root <repo> --json",
        ],
        "notes": [
            "This command is read-only.",
            "Candidate verification commands are reported but not executed.",
        ],
    },
    {
        "name": "explain-status",
        "summary": "Explain the latest status in operator-facing language.",
        "examples": [
            "agentteam explain-status --project-root <repo>",
            "agentteam explain-status --taskpack <id> --json",
        ],
    },
    {
        "name": "logs",
        "summary": "Tail compact runtime events for the latest or selected run.",
        "examples": [
            "agentteam logs --project-root <repo>",
            "agentteam logs --taskpack <id> --lines 10",
        ],
    },
    {
        "name": "db",
        "summary": "Rebuild or check the project-level artifact projection database.",
        "examples": [
            "agentteam db rebuild --project-root <repo>",
            "agentteam db check --project-root <repo> --json",
        ],
        "notes": [
            "The database is a rebuildable projection; file artifacts remain authoritative.",
        ],
    },
    {
        "name": "doctor",
        "summary": "Check project profile, git repository, runtime prerequisites, and verification profile.",
        "examples": [
            "agentteam doctor --project-root <repo>",
            "agentteam doctor --project-root <repo> --json",
            "agentteam doctor --invocation-supervision-probe --json",
        ],
        "notes": [
            "The invocation-supervision probe is the only doctor mode that creates a transient unit.",
            "The probe is bounded, invokes no provider, and cleans its transient unit before returning.",
        ],
    },
    {
        "name": "gc",
        "summary": "Clean AgentTeam local storage such as old runtime releases.",
        "examples": [
            "agentteam gc --project-root <repo>",
            "agentteam gc --project-root <repo> --global-releases",
            "agentteam gc --project-root <repo> --artifacts --json",
            "agentteam gc --project-root <repo> --artifacts --delete-artifacts --force --json",
            "agentteam gc --project-root <repo> --force",
        ],
        "notes": [
            "Without --force this command reports what it would manage without deleting releases.",
            "--delete-artifacts requires --artifacts --force and deletes only validated rebuildable candidates.",
            "Authoritative reports, events, patches, taskpacks, and state snapshots remain protected.",
        ],
    },
    {
        "name": "paths",
        "summary": "Show project, run, artifact, and integration baseline paths.",
        "examples": [
            "agentteam paths --project-root <repo>",
            "agentteam paths --taskpack <id> --json",
        ],
    },
    {
        "name": "integrate",
        "summary": "Fast-forward a completed run's integration baseline into the target repository.",
        "examples": [
            "agentteam integrate --project-root <repo> --taskpack <id>",
            "agentteam integrate --project-root <repo> --taskpack <id> --json",
        ],
        "notes": [
            "Requires a clean target repository.",
            "Only fast-forward merges are performed in this release.",
        ],
    },
    {
        "name": "notify",
        "summary": "Test project notification configuration without running a task.",
        "examples": [
            "agentteam notify test --project-root <repo>",
            "agentteam notify test --dry-run --json",
            "agentteam notify diagnose --project-root <repo> --dry-run --json",
            "agentteam notify run-completed --project-root <repo> --taskpack <id>",
        ],
        "subcommands": [
            "test: send a diagnostic Feishu notification from the current project profile",
            "diagnose: test rich and concise Feishu payload variants without exposing secrets",
            "run-completed: resend a completion summary for an existing run",
        ],
    },
    {
        "name": "report",
        "summary": "Render the latest run as a human-readable completion report.",
        "examples": [
            "agentteam report --project-root <repo>",
            "agentteam report --run-dir <run>",
        ],
    },
    {
        "name": "watch",
        "summary": "Print compact read-only progress lines for a run.",
        "examples": ["agentteam watch --project-root <repo> --max-lines 20"],
    },
    {
        "name": "chat",
        "summary": "Open a read-only diagnostic context for discussing a run.",
        "examples": [
            "agentteam chat --taskpack <id>",
            "agentteam chat --run-dir <run> --topic integration-failure",
            "agentteam chat --run-dir <run> --interactive",
        ],
        "notes": [
            "Defaults to printing the diagnostic context without launching a model.",
            "--interactive starts a Codex diagnostic session with the same read-only context.",
        ],
    },
    {
        "name": "stop",
        "summary": "Stop or clean up an existing run safely.",
        "examples": [
            "agentteam stop --project-root <repo>",
            "agentteam stop --project-root <repo> --authoring",
            "agentteam stop --project-root <repo> --stale",
        ],
        "notes": [
            "Signals only registered worker PIDs and owned descendants.",
            "--authoring stops a running Codex taskpack author recorded under work_root/drafts.",
            "--stale cleans stale state without terminating live processes.",
        ],
    },
    {
        "name": "continue",
        "summary": "Resume an existing frozen taskpack run.",
        "examples": [
            "agentteam continue --project-root <repo> --taskpack <id>",
            "agentteam continue --project-root <repo> --taskpack <id> --json",
        ],
    },
    {
        "name": "resume",
        "summary": "Interactively answer waiting manual gates.",
        "examples": ["agentteam resume --run-dir <run> --interactive"],
    },
    {
        "name": "answer",
        "summary": "Answer one manual gate directly by question id.",
        "examples": ["agentteam answer --run-dir <run> --question-id <id> --answer <text>"],
    },
    {
        "name": "permissions",
        "summary": "List, approve, or deny runtime permission requests.",
        "examples": [
            "agentteam permissions list --run-dir <run>",
            "agentteam permissions approve --run-dir <run> --request-id <id>",
            "agentteam permissions deny --run-dir <run> --request-id <id>",
        ],
        "subcommands": [
            "list: show waiting permission requests",
            "approve: clear the blocker and allow a bounded retry",
            "deny: keep the task blocked and record the decision",
        ],
    },
    {
        "name": "taskpack",
        "summary": "Draft, materialize, validate, freeze, list, and delete taskpacks.",
        "examples": [
            "agentteam taskpack new --goal \"profile algorithm latency\" --write-scope output/current/",
            "agentteam taskpack new --goal \"profile algorithm latency\" --write-scope output/current/ --freeze",
            "agentteam taskpack materialize <skeleton-dir> --semantic-json-file semantic.json --output-root <drafts> --freeze --frozen-root <frozen>",
            "agentteam taskpack list --project-root <repo>",
            "agentteam taskpack delete --project-root <repo> --taskpack <id> --dry-run",
            "agentteam taskpack delete --project-root <repo> --taskpack <id> --delete-run --force",
        ],
        "subcommands": [
            "new: create an explicit operator-authored taskpack from project profile defaults",
            "draft: create a taskpack from a goal without running it",
            "validate: validate a draft or frozen taskpack",
            "freeze: freeze an accepted draft for execution",
            "materialize: convert a semantic skeleton into an executable taskpack",
            "list: list frozen taskpacks and liveness-aware run status",
            "delete: remove draft/frozen taskpack files; run deletion requires --delete-run --force",
        ],
    },
    {
        "name": "update",
        "summary": "Manage AgentTeam runtime releases. New installs prune old completed-run releases by default.",
        "examples": [
            "agentteam update --project-root <repo> --status",
            "agentteam update --project-root <repo> --from <checkout> --release-id <id>",
            "agentteam update --project-root <repo> --adopt-run <id> --force",
            "agentteam update --project-root <repo> --prune",
            "agentteam update --project-root <repo> --rollback <release-id>",
        ],
    },
    {
        "name": "submit",
        "summary": "Lower-level command that drafts, freezes, and runs in one JSON flow.",
        "examples": ["agentteam submit --interactive"],
    },
    {
        "name": "run",
        "summary": "Lower-level command that runs an already frozen taskpack directory.",
        "examples": [
            "agentteam run <frozen-taskpack-dir> --run-root <runs-dir>",
            "agentteam run <frozen-taskpack-dir> --run-root <runs-dir> --json",
        ],
    },
]


_HELP_BY_NAME = {item["name"]: item for item in _HELP_COMMANDS}


def main(argv=None):
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        _validate_launcher_runtime_selection(args)
        result = args.handler(args)
        if isinstance(result, int):
            return result
        if result is not None:
            _print_json(result, stream=sys.stdout)
        return 0
    except AgentTeamCliError as exc:
        _print_json(_error_payload(exc), stream=sys.stderr)
        return 1
    except Exception as exc:
        _print_json(_error_payload(exc), stream=sys.stderr)
        return 1


def _build_parser():
    parser = JsonArgumentParser(description="AgentTeam operator CLI.")
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=JsonArgumentParser,
    )

    taskpack = subcommands.add_parser("taskpack", help="Draft, validate, and freeze taskpacks.")
    taskpack_subcommands = taskpack.add_subparsers(
        dest="taskpack_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    _add_submit_parser(subcommands)
    _add_help_parser(subcommands)
    _add_init_parser(subcommands)
    _add_start_parser(subcommands)
    _add_next_parser(subcommands)
    _add_queue_parser(subcommands)
    _add_pursue_parser(subcommands)
    _add_feedback_parser(subcommands)
    _add_taskpack_new_parser(taskpack_subcommands)
    _add_taskpack_draft_parser(taskpack_subcommands)
    _add_taskpack_validate_parser(taskpack_subcommands)
    _add_taskpack_freeze_parser(taskpack_subcommands)
    _add_taskpack_materialize_parser(taskpack_subcommands)
    _add_taskpack_list_parser(taskpack_subcommands)
    _add_taskpack_delete_parser(taskpack_subcommands)
    _add_run_parser(subcommands)
    _add_continue_parser(subcommands)
    _add_answer_parser(subcommands)
    _add_permissions_parser(subcommands)
    _add_resume_parser(subcommands)
    _add_watch_parser(subcommands)
    _add_report_parser(subcommands)
    _add_chat_parser(subcommands)
    _add_db_parser(subcommands)
    _add_doctor_parser(subcommands)
    _add_grounding_parser(subcommands)
    _add_logs_parser(subcommands)
    _add_explain_status_parser(subcommands)
    _add_gc_parser(subcommands)
    _add_stop_parser(subcommands)
    _add_update_parser(subcommands)
    _add_paths_parser(subcommands)
    _add_gate_parser(subcommands)
    _add_integrate_parser(subcommands)
    _add_notify_parser(subcommands)
    _add_stats_parser(subcommands)
    _add_status_parser(subcommands)
    return parser


def _add_help_parser(subcommands):
    parser = subcommands.add_parser("help", help="Show AgentTeam command guidance.")
    parser.add_argument("topic", nargs="?", help="Optional command name, for example: stop or taskpack.")
    parser.set_defaults(handler=_handle_help)


def _add_submit_parser(subcommands):
    parser = subcommands.add_parser("submit", help="Draft, validate, freeze, and run a taskpack.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for submit inputs interactively. Prompts are written to stderr.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project.")
    parser.add_argument("--goal", help="Human-readable taskpack goal.")
    parser.add_argument("--work-root", help="Directory for drafts, frozen taskpacks, and runs.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        default="fake",
        help="Runtime used to author the taskpack.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        default="auto",
        help="Runtime backend used to execute the frozen taskpack.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    _add_codex_model_arg(parser)
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Use the one-shot scheduler path instead of the daemon worker-pool path.",
    )
    parser.add_argument("--max-inflight", type=int, default=2, help="Maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Commit integration worktree changes after verification passes.",
    )
    _add_notification_args(parser)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.set_defaults(handler=_handle_submit)


def _add_init_parser(subcommands):
    parser = subcommands.add_parser("init", help="Create a project-local .agentteam profile.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for profile fields. Prompts are written to stderr.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project.")
    parser.add_argument("--project-key", help="Stable key used for AgentTeam local storage.")
    parser.add_argument("--work-root", help="Directory for drafts, frozen taskpacks, and runs.")
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        default="codex",
        help="Runtime used to author taskpacks for this project.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        default="auto",
        help="Runtime backend used to execute taskpacks for this project.",
    )
    _add_codex_model_arg(parser, help_text="Default Codex model used by worker runtimes.")
    parser.add_argument("--one-shot", action="store_true", help="Default to one-shot runtime execution.")
    parser.add_argument("--max-inflight", type=int, default=2, help="Default maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Default maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Default to committing integration worktree changes after verification passes.",
    )
    parser.add_argument(
        "--verification-command-json",
        help="Project correctness verification command as a JSON string array.",
    )
    parser.add_argument(
        "--performance-command-json",
        help="Optional project performance benchmark command as a JSON string array.",
    )
    parser.add_argument(
        "--metric",
        action="append",
        help="Performance or quality metric name tracked by the project. Repeat for multiple metrics.",
    )
    _add_notification_args(parser, notification_project_default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .agentteam/profile.json.",
    )
    parser.add_argument("--json", action="store_true", help="Print full profile details as JSON.")
    parser.set_defaults(handler=_handle_init)


def _add_start_parser(subcommands):
    parser = subcommands.add_parser("start", help="Start AgentTeam from the current project profile.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--goal", help="Human-readable taskpack goal. Prompted when omitted.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument("--work-root", help="Override the profile work root for this run.")
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        help="Override the profile taskpack author runtime.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        help="Override the profile execution runtime.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    _add_codex_model_arg(parser)
    parser.add_argument(
        "--one-shot",
        action="store_true",
        default=None,
        help="Use the one-shot scheduler path for this run.",
    )
    parser.add_argument("--max-inflight", type=int, help="Override maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, help="Override maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        default=None,
        help="Commit integration worktree changes after verification passes for this run.",
    )
    _add_notification_args(parser, notification_project_default=None)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full execution result as JSON.")
    parser.set_defaults(handler=_handle_start)


def _add_next_parser(subcommands):
    parser = subcommands.add_parser("next", help="Create and run a follow-up taskpack from a previous run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--from-taskpack", help="Source taskpack/run id. Defaults to the latest run.")
    parser.add_argument("--from-run-dir", help="Source run directory. Overrides --from-taskpack.")
    parser.add_argument("--goal", help="Follow-up goal. Prompted when omitted.")
    parser.add_argument("--taskpack-id", help="Optional safe id for the new follow-up taskpack.")
    parser.add_argument("--work-root", help="Override the profile work root for this run.")
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        help="Override the profile taskpack author runtime.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        help="Override the profile execution runtime.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    _add_codex_model_arg(parser)
    parser.add_argument(
        "--one-shot",
        action="store_true",
        default=None,
        help="Use the one-shot scheduler path for this run.",
    )
    parser.add_argument("--max-inflight", type=int, help="Override maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, help="Override maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        default=None,
        help="Commit integration worktree changes after verification passes for this run.",
    )
    _add_notification_args(parser, notification_project_default=None)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full execution result as JSON.")
    parser.set_defaults(handler=_handle_next)


def _add_queue_parser(subcommands):
    parser = subcommands.add_parser(
        "queue",
        help="Inspect the suggested follow-up queue for a completed run.",
    )
    queue_subcommands = parser.add_subparsers(
        dest="queue_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    for command_name, help_text in [
        ("show", "Show all bounded follow-up queue items."),
        ("next", "Show only the next suggested follow-up goal and command."),
    ]:
        item_parser = queue_subcommands.add_parser(command_name, help=help_text)
        item_parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
        item_parser.add_argument("--taskpack", help="Source taskpack/run id. Defaults to the latest run.")
        item_parser.add_argument("--run-dir", help="Existing source run directory. Overrides --taskpack.")
        item_parser.add_argument("--limit", type=int, default=5, help="Maximum queue items to show.")
        item_parser.add_argument("--json", action="store_true", help="Print queue summary as JSON.")
        item_parser.set_defaults(handler=_handle_queue)


def _add_pursue_parser(subcommands):
    parser = subcommands.add_parser(
        "pursue",
        help="Run a bounded long-goal loop across start and follow-up taskpacks.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--goal", help="Long-running goal. Prompted when omitted.")
    parser.add_argument("--taskpack-id", help="Optional safe id for the first taskpack.")
    parser.add_argument("--work-root", help="Override the profile work root for this pursue run.")
    parser.add_argument("--max-rounds", type=int, default=3, help="Maximum taskpack rounds to run.")
    review_gate = parser.add_mutually_exclusive_group()
    review_gate.add_argument(
        "--stop-on-review-gate",
        action="store_true",
        default=True,
        help="Stop when a run produces an integration/source review gate. This is the default.",
    )
    review_gate.add_argument(
        "--allow-review-gate-follow-up",
        action="store_true",
        help="Allow follow-up authoring even when the previous run has an integration review gate.",
    )
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        help="Override the profile taskpack author runtime.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        help="Override the profile execution runtime.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    _add_codex_model_arg(parser)
    parser.add_argument(
        "--one-shot",
        action="store_true",
        default=None,
        help="Use the one-shot scheduler path for each round.",
    )
    parser.add_argument("--max-inflight", type=int, help="Override maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, help="Override maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        default=None,
        help="Commit integration worktree changes after verification passes for each round.",
    )
    _add_notification_args(parser, notification_project_default=None)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full pursue result as JSON.")
    parser.set_defaults(handler=_handle_pursue)


def _add_feedback_parser(subcommands):
    parser = subcommands.add_parser(
        "feedback",
        help="Create or inspect semantic feedback proposal artifacts.",
    )
    feedback_subcommands = parser.add_subparsers(
        dest="feedback_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    propose = feedback_subcommands.add_parser(
        "propose",
        help="Write a semantic feedback proposal from an implementation run.",
    )
    propose.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    propose.add_argument("--taskpack", help="Source taskpack/run id. Defaults to latest run.")
    propose.add_argument("--run-dir", help="Existing source run directory. Overrides --taskpack.")
    propose.add_argument("--proposal-id", required=True, help="Stable proposal artifact id.")
    propose.add_argument(
        "--target-artifact",
        action="append",
        required=True,
        help="Semantic authority artifact path to review. Repeat for multiple targets.",
    )
    propose.add_argument("--summary", required=True, help="Short natural-language proposal summary.")
    propose.add_argument("--rationale", required=True, help="Evidence-backed reason for the proposal.")
    propose.add_argument("--json", action="store_true", help="Print proposal as JSON.")
    propose.set_defaults(handler=_handle_feedback)

    list_parser = feedback_subcommands.add_parser(
        "list",
        help="List semantic feedback proposals for the project.",
    )
    list_parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    list_parser.add_argument("--json", action="store_true", help="Print proposal list as JSON.")
    list_parser.set_defaults(handler=_handle_feedback)


def _add_taskpack_new_parser(subcommands):
    parser = subcommands.add_parser(
        "new",
        help="Create an explicit operator-authored taskpack from project profile defaults.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--work-root", help="Override the project profile work root.")
    parser.add_argument("--goal", help="Human-readable taskpack goal. Prompted when omitted.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument(
        "--read-scope",
        action="append",
        help="Repository-relative read scope. Repeat for multiple scopes. Defaults to '.'.",
    )
    parser.add_argument(
        "--write-scope",
        action="append",
        help="Repository-relative write scope. Repeat for multiple scopes.",
    )
    parser.add_argument(
        "--verification-command-json",
        help="Verification command as a JSON string array. Defaults to python3 unittest discovery.",
    )
    parser.add_argument("--allow-merge", action="store_true", help="Set taskpack policy.allow_merge.")
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=1800,
        help="Worker Codex timeout recorded in the taskpack runtime profile.",
    )
    _add_codex_model_arg(parser)
    parser.add_argument("--freeze", action="store_true", help="Freeze the draft immediately after validation.")
    parser.add_argument("--json", action="store_true", help="Print result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_taskpack_new)


def _add_taskpack_draft_parser(subcommands):
    parser = subcommands.add_parser("draft", help="Draft a taskpack from a human goal.")
    parser.add_argument("--project-root", required=True, help="Git repository root for the target project.")
    parser.add_argument("--goal", required=True, help="Human-readable taskpack goal.")
    parser.add_argument("--draft-root", required=True, help="Directory where the draft taskpack will be written.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument(
        "--author-runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        default="fake",
        help="Runtime used to author the taskpack.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.set_defaults(handler=_handle_taskpack_draft)


def _add_codex_model_arg(parser, help_text=None):
    parser.add_argument(
        "--codex-model",
        help=help_text or "Codex model used by worker runtimes for newly generated taskpacks.",
    )


def _add_taskpack_validate_parser(subcommands):
    parser = subcommands.add_parser("validate", help="Validate a draft or frozen taskpack.")
    parser.add_argument("taskpack_dir", help="Taskpack directory to validate.")
    parser.set_defaults(handler=_handle_taskpack_validate)


def _add_taskpack_freeze_parser(subcommands):
    parser = subcommands.add_parser("freeze", help="Freeze an accepted taskpack for runtime launch.")
    parser.add_argument("taskpack_dir", help="Draft taskpack directory to freeze.")
    parser.add_argument("--frozen-root", required=True, help="Directory where frozen taskpacks are written.")
    parser.set_defaults(handler=_handle_taskpack_freeze)


def _add_taskpack_materialize_parser(subcommands):
    parser = subcommands.add_parser(
        "materialize",
        help="Convert a semantic skeleton or tracked blueprint into an executable taskpack.",
    )
    parser.add_argument(
        "skeleton_taskpack_dir",
        nargs="?",
        help="Deterministic skeleton taskpack directory (semantic sources only).",
    )
    parser.add_argument(
        "--project-root",
        help="Git repository root for blueprint materialization. Defaults to cwd.",
    )
    parser.add_argument("--output-root", required=True, help="Directory where the executable draft is written.")
    parser.add_argument("--taskpack-id", help="Optional id for the executable taskpack.")
    semantic_source = parser.add_mutually_exclusive_group(required=True)
    semantic_source.add_argument("--semantic-json", help="Semantic completion JSON object.")
    semantic_source.add_argument("--semantic-json-file", help="Path to a semantic completion JSON object.")
    semantic_source.add_argument(
        "--blueprint-file",
        help="Tracked agentteam_taskpack_blueprint.v1 JSON file.",
    )
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument("--dry-run", action="store_true", help="Validate a blueprint without retaining output.")
    retention.add_argument("--freeze", action="store_true", help="Freeze the materialized taskpack immediately.")
    parser.add_argument("--frozen-root", help="Directory where frozen taskpacks are written when --freeze is set.")
    parser.add_argument("--json", action="store_true", help="Print result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_taskpack_materialize)


def _add_taskpack_list_parser(subcommands):
    parser = subcommands.add_parser("list", help="List frozen taskpacks for a project.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--work-root", help="Override the project profile work root.")
    parser.add_argument("--json", action="store_true", help="Print taskpacks as JSON instead of human text.")
    parser.set_defaults(handler=_handle_taskpack_list)


def _add_taskpack_delete_parser(subcommands):
    parser = subcommands.add_parser("delete", help="Delete a draft/frozen taskpack and optionally its run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--work-root", help="Override the project profile work root.")
    parser.add_argument("--taskpack", required=True, help="Taskpack id to delete.")
    parser.add_argument("--delete-run", action="store_true", help="Also delete the run directory.")
    parser.add_argument("--force", action="store_true", help="Required for non-dry-run deletion.")
    parser.add_argument("--dry-run", action="store_true", help="Report delete candidates without mutating files.")
    parser.add_argument("--json", action="store_true", help="Print delete result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_taskpack_delete)


def _add_run_parser(subcommands):
    parser = subcommands.add_parser("run", help="Run a frozen taskpack through agentteam_runtime.cli.")
    parser.add_argument("frozen_taskpack_dir", help="Frozen taskpack directory to run.")
    parser.add_argument("--run-root", required=True, help="Directory where run output will be written.")
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Use the one-shot scheduler path instead of the daemon worker-pool path.",
    )
    parser.add_argument("--max-inflight", type=int, default=2, help="Maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Commit integration worktree changes after verification passes.",
    )
    _add_notification_args(parser)
    parser.add_argument("--json", action="store_true", help="Print the full run result as JSON.")
    parser.set_defaults(handler=_handle_run)


def _add_continue_parser(subcommands):
    parser = subcommands.add_parser("continue", help="Continue an existing frozen taskpack run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Frozen taskpack id to continue. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to continue. Defaults to the selected taskpack run.")
    parser.add_argument(
        "--one-shot",
        action="store_true",
        default=None,
        help="Use the one-shot scheduler path for this continue run.",
    )
    parser.add_argument("--max-inflight", type=int, help="Override maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, help="Override maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        default=None,
        help="Commit integration worktree changes after verification passes.",
    )
    _add_notification_args(parser, notification_project_default=None)
    parser.add_argument("--json", action="store_true", help="Print the full continue result as JSON.")
    parser.set_defaults(handler=_handle_continue)


def _add_notification_args(parser, notification_project_default="agentteam"):
    parser.add_argument(
        "--notification-project",
        default=notification_project_default,
        help="Project key recorded in outbound notification telemetry.",
    )
    parser.add_argument(
        "--feishu-webhook-env",
        help="Environment variable containing the Feishu custom bot webhook URL.",
    )
    parser.add_argument(
        "--feishu-signing-secret-env",
        help="Optional environment variable containing the Feishu custom bot signing secret.",
    )


def _add_answer_parser(subcommands):
    parser = subcommands.add_parser("answer", help="Answer a runtime manual gate and resume its task.")
    parser.add_argument("--run-dir", required=True, help="Runtime output directory containing events.jsonl.")
    parser.add_argument("--question-id", required=True, help="Manual gate question id to answer.")
    parser.add_argument("--answer", required=True, help="Operator answer text.")
    parser.add_argument("--operator", default="operator", help="Operator identity recorded in the event log.")
    parser.set_defaults(handler=_handle_answer)


def _add_permissions_parser(subcommands):
    parser = subcommands.add_parser(
        "permissions",
        help="List or resolve runtime permission requests.",
    )
    permission_subcommands = parser.add_subparsers(
        dest="permission_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    list_parser = permission_subcommands.add_parser(
        "list",
        help="List waiting runtime permission requests.",
    )
    list_parser.add_argument("--run-dir", required=True, help="Runtime output directory containing events.jsonl.")
    list_parser.add_argument("--json", action="store_true", help="Print list result as JSON.")
    list_parser.set_defaults(handler=_handle_permissions)

    approve_parser = permission_subcommands.add_parser(
        "approve",
        help="Approve a waiting runtime permission request.",
    )
    approve_parser.add_argument("--run-dir", required=True, help="Runtime output directory containing events.jsonl.")
    approve_parser.add_argument("--request-id", required=True, help="Permission request id to approve.")
    approve_parser.add_argument("--operator", default="operator", help="Operator identity recorded in the event log.")
    approve_parser.add_argument("--reason", help="Reason recorded with the approval.")
    approve_parser.set_defaults(handler=_handle_permissions)

    deny_parser = permission_subcommands.add_parser(
        "deny",
        help="Deny a waiting runtime permission request.",
    )
    deny_parser.add_argument("--run-dir", required=True, help="Runtime output directory containing events.jsonl.")
    deny_parser.add_argument("--request-id", required=True, help="Permission request id to deny.")
    deny_parser.add_argument("--operator", default="operator", help="Operator identity recorded in the event log.")
    deny_parser.add_argument("--reason", help="Reason recorded with the denial.")
    deny_parser.set_defaults(handler=_handle_permissions)


def _add_resume_parser(subcommands):
    parser = subcommands.add_parser("resume", help="Interactively answer waiting runtime manual gates.")
    parser.add_argument("--run-dir", required=True, help="Runtime output directory containing events.jsonl.")
    parser.add_argument(
        "--question-id",
        help="Optional manual gate question id. When omitted, all waiting gates are prompted in order.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List waiting manual gates as JSON without answering them.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for answers to waiting manual gates. Prompts are written to stderr.",
    )
    parser.add_argument("--operator", default="operator", help="Operator identity recorded in the event log.")
    parser.set_defaults(handler=_handle_resume)


def _add_stop_parser(subcommands):
    parser = subcommands.add_parser("stop", help="Stop or clean up an existing AgentTeam run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to stop. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to stop. Defaults to selected profile run.")
    parser.add_argument(
        "--stale",
        action="store_true",
        help="Only repair stale running state whose registered PIDs are no longer alive.",
    )
    parser.add_argument(
        "--authoring",
        action="store_true",
        help="Stop the latest live Codex taskpack author instead of a runtime run.",
    )
    parser.add_argument("--grace-seconds", type=int, default=5, help="Seconds to wait after SIGTERM.")
    parser.add_argument("--force", action="store_true", help="Send SIGKILL if registered PIDs do not exit.")
    parser.add_argument("--operator", default="operator", help="Operator identity recorded in state updates.")
    parser.add_argument("--json", action="store_true", help="Print stop result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_stop)


def _add_watch_parser(subcommands):
    parser = subcommands.add_parser("watch", help="Watch compact progress for an AgentTeam run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to watch. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to watch. Defaults to selected profile run.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between progress lines.")
    parser.add_argument("--max-lines", type=int, help="Maximum lines to print before exiting.")
    parser.add_argument("--json-lines", action="store_true", help="Print progress as JSON lines.")
    parser.set_defaults(handler=_handle_watch)


def _add_report_parser(subcommands):
    parser = subcommands.add_parser("report", help="Show a human-readable AgentTeam run report.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to report. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to report. Overrides --project-root selection.")
    parser.add_argument("--json", action="store_true", help="Print report metadata as JSON instead of markdown.")
    parser.set_defaults(handler=_handle_report)


def _add_chat_parser(subcommands):
    parser = subcommands.add_parser("chat", help="Discuss a run with a read-only diagnostic agent context.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to inspect. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to inspect. Overrides --project-root selection.")
    parser.add_argument(
        "--topic",
        default="runtime-diagnostic",
        help="Diagnostic topic label, for example integration-failure or patch-review.",
    )
    parser.add_argument("--json", action="store_true", help="Print the diagnostic context as JSON.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch Codex with the diagnostic context instead of only printing it.",
    )
    parser.add_argument("--codex-model", help="Optional model passed to Codex for interactive diagnostics.")
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=DEFAULT_CODEX_TIMEOUT_SECONDS,
        help="Timeout for the Codex diagnostic subprocess.",
    )
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.set_defaults(handler=_handle_chat)


def _add_db_parser(subcommands):
    parser = subcommands.add_parser(
        "db",
        help="Rebuild or check the project artifact projection database.",
    )
    db_subcommands = parser.add_subparsers(
        dest="db_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    rebuild = db_subcommands.add_parser(
        "rebuild",
        help="Rebuild <work_root>/agentteam.db from authoritative files.",
    )
    rebuild.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    rebuild.add_argument("--json", action="store_true", help="Print rebuild summary as JSON.")
    rebuild.set_defaults(handler=_handle_db)

    check = db_subcommands.add_parser(
        "check",
        help="Check whether <work_root>/agentteam.db matches authoritative files.",
    )
    check.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    check.add_argument("--json", action="store_true", help="Print check summary as JSON.")
    check.set_defaults(handler=_handle_db)


def _add_doctor_parser(subcommands):
    parser = subcommands.add_parser("doctor", help="Check AgentTeam project configuration and prerequisites.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument(
        "--invocation-supervision-probe",
        action="store_true",
        help="Run the bounded no-provider Linux pidfd/systemd-user lifecycle probe.",
    )
    parser.add_argument("--json", action="store_true", help="Print doctor checks as JSON instead of human text.")
    parser.set_defaults(handler=_handle_doctor)


def _add_grounding_parser(subcommands):
    parser = subcommands.add_parser(
        "grounding",
        help="Summarize repository languages, project tools, tests, and candidate verification commands.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--json", action="store_true", help="Print grounding summary as JSON instead of human text.")
    parser.set_defaults(handler=_handle_grounding)


def _add_logs_parser(subcommands):
    parser = subcommands.add_parser("logs", help="Tail compact events for an AgentTeam run.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to inspect. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to inspect. Overrides --taskpack.")
    parser.add_argument("--lines", type=int, default=20, help="Number of latest events to print.")
    parser.add_argument("--json", action="store_true", help="Print events as JSON instead of human text.")
    parser.set_defaults(handler=_handle_logs)


def _add_explain_status_parser(subcommands):
    parser = subcommands.add_parser("explain-status", help="Explain the current AgentTeam status.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to explain. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to explain. Overrides --taskpack.")
    parser.add_argument("--json", action="store_true", help="Print explanation as JSON instead of human text.")
    parser.set_defaults(handler=_handle_explain_status)


def _add_gc_parser(subcommands):
    parser = subcommands.add_parser("gc", help="Clean AgentTeam local storage for a project.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--keep-releases", type=int, default=1, help="Number of latest releases to retain.")
    parser.add_argument("--stale-runs", action="store_true", help="Also repair stale running run state.")
    parser.add_argument("--global-releases", action="store_true", help="Also scan the shared global runtime release store.")
    parser.add_argument("--artifacts", action="store_true", help="Also include a read-only artifact retention plan.")
    parser.add_argument(
        "--delete-artifacts",
        action="store_true",
        help="With --artifacts --force, delete validated rebuildable artifact candidates.",
    )
    parser.add_argument("--artifact-limit", type=int, default=20, help="Maximum rebuildable artifact candidates to list.")
    parser.add_argument("--force", action="store_true", help="Actually delete eligible old releases.")
    parser.add_argument("--json", action="store_true", help="Print cleanup result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_gc)


def _add_paths_parser(subcommands):
    parser = subcommands.add_parser("paths", help="Show AgentTeam project and run paths.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to inspect. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to inspect. Overrides --project-root selection.")
    parser.add_argument("--json", action="store_true", help="Print paths as JSON instead of human text.")
    parser.set_defaults(handler=_handle_paths)


def _add_gate_parser(subcommands):
    parser = subcommands.add_parser(
        "gate",
        help="Manage file-authoritative post-backlog integration gates.",
    )
    gate_commands = parser.add_subparsers(
        dest="gate_command",
        required=True,
        parser_class=JsonArgumentParser,
    )

    seal = gate_commands.add_parser(
        "seal-baseline",
        help="Rerun frozen verification and publish the first gate epoch.",
    )
    _add_gate_run_selection_arguments(seal)
    seal.add_argument("--expected-integration-head", required=True)
    seal.add_argument("--authorize-revalidation", action="store_true", required=True)
    seal.add_argument("--json", action="store_true")
    seal.set_defaults(handler=_handle_gate)

    refresh = gate_commands.add_parser(
        "refresh-baseline",
        help="Merge a newly resolved target into a fresh immutable gate epoch.",
    )
    _add_gate_run_selection_arguments(refresh)
    refresh.add_argument("--expected-gate-epoch", required=True, type=int)
    refresh.add_argument("--expected-target-head", required=True)
    refresh.add_argument("--authorize-revalidation", action="store_true", required=True)
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(handler=_handle_gate)

    register = gate_commands.add_parser(
        "register",
        help="Register the epoch-scoped evidence run for a gate.",
    )
    _add_gate_run_selection_arguments(register)
    register.add_argument("--gate", required=True)
    register.add_argument("--gate-epoch", required=True, type=int)
    register.add_argument("--evidence-run", required=True)
    register.add_argument("--expected-integration-head", required=True)
    register.add_argument("--json", action="store_true")
    register.set_defaults(handler=_handle_gate)

    approve = gate_commands.add_parser(
        "approve",
        help="Interactively publish an immutable operator gate approval.",
    )
    _add_gate_run_selection_arguments(approve)
    approve.add_argument("--gate", required=True)
    approve.add_argument("--gate-epoch", required=True, type=int)
    approve.add_argument("--expected-evidence-sha256", required=True)
    approve.add_argument("--expected-integration-head", required=True)
    approve.add_argument("--approve", action="store_true", required=True)
    approve.add_argument("--json", action="store_true")
    approve.set_defaults(handler=_handle_gate)


def _add_gate_run_selection_arguments(parser):
    parser.add_argument("--project-root", help="Git repository root. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Implementation run/taskpack id. Defaults to latest.")
    parser.add_argument("--run-dir", help="Existing implementation run directory.")


def _add_integrate_parser(subcommands):
    parser = subcommands.add_parser(
        "integrate",
        help="Fast-forward a run integration baseline into the target repository.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to integrate. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to integrate. Overrides --taskpack.")
    parser.add_argument(
        "--rebase",
        action="store_true",
        help="Rebase the integration baseline onto the current target HEAD before merging.",
    )
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="Record that the operator handled this baseline without merging it.",
    )
    parser.add_argument("--json", action="store_true", help="Print integration result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_integrate)


def _add_notify_parser(subcommands):
    parser = subcommands.add_parser(
        "notify",
        help="Test or inspect project notification configuration.",
    )
    notify_subcommands = parser.add_subparsers(
        dest="notify_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    test_parser = notify_subcommands.add_parser(
        "test",
        help="Send a diagnostic Feishu notification using the current project profile.",
    )
    test_parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    test_parser.add_argument("--notification-project", help="Project label used in the notification.")
    test_parser.add_argument("--feishu-webhook-env", help="Override the profile Feishu webhook env var name.")
    test_parser.add_argument("--feishu-signing-secret-env", help="Override the profile Feishu signing secret env var name.")
    test_parser.add_argument("--message", help="Optional diagnostic message body.")
    test_parser.add_argument("--dry-run", action="store_true", help="Validate configuration without sending.")
    test_parser.add_argument("--json", action="store_true", help="Print notification test result as JSON.")
    test_parser.set_defaults(handler=_handle_notify)

    diagnose_parser = notify_subcommands.add_parser(
        "diagnose",
        help="Diagnose Feishu webhook payload variants using the current project profile.",
    )
    diagnose_parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    diagnose_parser.add_argument("--notification-project", help="Project label used in the notification.")
    diagnose_parser.add_argument("--feishu-webhook-env", help="Override the profile Feishu webhook env var name.")
    diagnose_parser.add_argument(
        "--feishu-signing-secret-env",
        help="Override the profile Feishu signing secret env var name.",
    )
    diagnose_parser.add_argument("--message", help="Optional diagnostic message body.")
    diagnose_parser.add_argument("--dry-run", action="store_true", help="Validate payload variants without sending.")
    diagnose_parser.add_argument("--json", action="store_true", help="Print notification diagnosis as JSON.")
    diagnose_parser.set_defaults(handler=_handle_notify)

    run_completed_parser = notify_subcommands.add_parser(
        "run-completed",
        help="Send or resend a run_completed notification for an existing run.",
    )
    run_completed_parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    run_completed_parser.add_argument("--taskpack", help="Run/taskpack id to notify. Defaults to latest run id.")
    run_completed_parser.add_argument("--run-dir", help="Existing run directory to notify. Overrides --taskpack.")
    run_completed_parser.add_argument("--notification-project", help="Project label used in the notification.")
    run_completed_parser.add_argument("--feishu-webhook-env", help="Override the profile Feishu webhook env var name.")
    run_completed_parser.add_argument(
        "--feishu-signing-secret-env",
        help="Override the profile Feishu signing secret env var name.",
    )
    run_completed_parser.add_argument("--dry-run", action="store_true", help="Build notification payload without sending.")
    run_completed_parser.add_argument("--json", action="store_true", help="Print notification result as JSON.")
    run_completed_parser.set_defaults(handler=_handle_notify)


def _add_status_parser(subcommands):
    parser = subcommands.add_parser("status", help="Show the latest AgentTeam run status for a project.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--run-dir", help="Specific run directory to summarize. Defaults to latest profile run.")
    parser.add_argument("--json", action="store_true", help="Print status as JSON instead of human text.")
    parser.set_defaults(handler=_handle_status)


def _add_stats_parser(subcommands):
    parser = subcommands.add_parser("stats", help="Show AgentTeam project-level statistics.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--json", action="store_true", help="Print stats as JSON instead of human text.")
    parser.set_defaults(handler=_handle_stats)


def _add_update_parser(subcommands):
    parser = subcommands.add_parser("update", help="Manage side-by-side AgentTeam runtime releases.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Show active release, known releases, and run bindings.")
    action.add_argument("--from", dest="source_checkout", help="Install and activate a release from a clean checkout.")
    action.add_argument("--from-git", dest="source_git", help="Install and activate a release from a git repository ref.")
    action.add_argument("--activate", help="Activate an already installed release id.")
    action.add_argument("--rollback", help="Activate an older release id.")
    action.add_argument("--prune", action="store_true", help="Prune old installed releases, keeping the active/latest release.")
    action.add_argument(
        "--adopt-run",
        help="Atomically bind an explicit unbound legacy run to a selected runtime release.",
    )
    parser.add_argument("--ref", dest="source_ref", help="Git ref to use with --from-git.")
    parser.add_argument(
        "--release-id",
        help="Release id for install or legacy adoption. Adoption defaults to the active release.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Confirm the immutable legacy-run adoption operation.",
    )
    parser.add_argument("--json", action="store_true", help="Print update result as JSON instead of human text.")
    parser.set_defaults(handler=_handle_update)


def _handle_taskpack_draft(args):
    return draft_taskpack_from_goal(
        project_root=args.project_root,
        goal=args.goal,
        draft_root=args.draft_root,
        author_runtime=args.author_runtime,
        taskpack_id=args.taskpack_id,
        codex_command=args.codex_command,
        codex_timeout_seconds=args.codex_timeout_seconds,
    )


def _handle_help(args):
    if args.topic:
        topic = args.topic.strip()
        details = _HELP_BY_NAME.get(topic)
        if not details:
            raise AgentTeamCliError(
                "unknown help topic",
                topic=topic,
                available=[item["name"] for item in _HELP_COMMANDS],
            )
        _write_help_detail(details)
        return 0
    _write_help_index()
    return 0


def _write_help_index():
    lines = [
        "AgentTeam commands",
        "",
        "Common workflow:",
        "  1. agentteam init --interactive",
        "  2. agentteam start",
        "  3. agentteam status",
        "  4. agentteam watch",
        "  5. agentteam stop or agentteam continue when needed",
        "",
        "Commands:",
    ]
    width = max(len(item["name"]) for item in _HELP_COMMANDS)
    for item in _HELP_COMMANDS:
        lines.append(f"  {item['name']:<{width}}  {item['summary']}")
    lines.extend(
        [
            "",
            "Run `agentteam help <command>` for details.",
            "Run `agentteam <command> --help` for exact flags.",
        ]
    )
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_help_detail(details):
    lines = [
        f"agentteam {details['name']}",
        f"Meaning: {details['summary']}",
    ]
    subcommands = details.get("subcommands") or []
    if subcommands:
        lines.extend(["", "Subcommands:"])
        lines.extend(f"  - {item}" for item in subcommands)
    examples = details.get("examples") or []
    if examples:
        lines.extend(["", "Examples:"])
        lines.extend(f"  {item}" for item in examples)
    notes = details.get("notes") or []
    if notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"  - {item}" for item in notes)
    lines.append("")
    lines.append(f"Run `agentteam {details['name']} --help` for exact flags.")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_taskpack_validate(args):
    return validate_taskpack(args.taskpack_dir)


def _handle_taskpack_new(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    if args.work_root:
        profile = {**profile, "work_root": str(Path(args.work_root).resolve())}
    work_root = Path(profile["work_root"]).resolve()
    draft_root = work_root / "drafts"
    frozen_root = work_root / "frozen"
    goal = args.goal or _prompt_text("Goal", required=True)
    write_scope = args.write_scope
    if not write_scope:
        write_scope_text = _prompt_text("Write scope", required=True)
        write_scope = [write_scope_text]
    verification_profile = effective_project_verification_profile(
        project_root,
        profile.get("verification_profile"),
    )
    default_verification_command = _profile_correctness_command(verification_profile)
    verification_command = _parse_verification_command_arg(
        args.verification_command_json,
        default=default_verification_command,
    )
    draft = draft_taskpack_files(
        project_root=project_root,
        goal=goal,
        draft_root=draft_root,
        taskpack_id=args.taskpack_id,
        read_scope=args.read_scope or ["."],
        write_scope=write_scope,
        verification_command=verification_command,
        verification_profile=verification_profile,
        allow_merge=args.allow_merge,
        codex_timeout_seconds=args.codex_timeout_seconds,
        codex_model=getattr(args, "codex_model", None) or profile.get("codex_model"),
    )
    validation = validate_taskpack(draft["taskpack_dir"])
    frozen = None
    if args.freeze:
        frozen = freeze_taskpack(draft["taskpack_dir"], frozen_root)
    summary = {
        "new_status": "frozen" if frozen else "draft",
        "taskpack_id": draft["taskpack_id"],
        "project": profile.get("project_key") or project_root.name,
        "draft": draft,
        "validation": validation,
        "frozen": frozen,
        "paths": {
            "work_root": str(work_root),
            "draft_root": str(draft_root),
            "frozen_root": str(frozen_root),
        },
    }
    if args.json:
        return summary
    _write_taskpack_new_text(summary)
    return 0


def _parse_verification_command_arg(raw_command, default):
    if not raw_command:
        return list(default)
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as exc:
        raise AgentTeamCliError(
            "--verification-command-json must be valid JSON",
            error=str(exc),
        ) from exc
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise AgentTeamCliError("--verification-command-json must be a non-empty string array")
    return command


def _verification_profile_from_args(args):
    profile = {}
    verification_command_json = getattr(args, "verification_command_json", None)
    performance_command_json = getattr(args, "performance_command_json", None)
    metrics = getattr(args, "metric", None) or []
    if verification_command_json:
        profile["correctness"] = {
            "command": _parse_command_json_arg(
                verification_command_json,
                "--verification-command-json",
            )
        }
    if performance_command_json or metrics:
        performance = {"metrics": list(metrics)}
        if performance_command_json:
            performance["command"] = _parse_command_json_arg(
                performance_command_json,
                "--performance-command-json",
            )
        profile["performance"] = performance
    return profile


def _parse_command_json_arg(raw_command, flag):
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as exc:
        raise AgentTeamCliError(f"{flag} must be valid JSON", error=str(exc)) from exc
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise AgentTeamCliError(f"{flag} must be a non-empty string array")
    return command


def _profile_correctness_command(verification_profile):
    if isinstance(verification_profile, dict):
        correctness = verification_profile.get("correctness")
        if isinstance(correctness, dict):
            command = correctness.get("command")
            if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
                return command
    return ["python3", "-m", "unittest", "discover"]


def _write_taskpack_new_text(summary):
    lines = [
        f"taskpack_id: {summary['taskpack_id']}",
        f"new_status: {summary['new_status']}",
        f"draft_dir: {summary['draft']['taskpack_dir']}",
    ]
    frozen = summary.get("frozen")
    if isinstance(frozen, dict):
        lines.append(f"frozen_dir: {frozen['frozen_taskpack_dir']}")
    lines.append(f"validation: {summary['validation']['status']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_taskpack_freeze(args):
    return freeze_taskpack(args.taskpack_dir, args.frozen_root)


def _handle_taskpack_materialize(args):
    freeze = bool(getattr(args, "freeze", False))
    dry_run = bool(getattr(args, "dry_run", False))
    frozen_root = getattr(args, "frozen_root", None)
    blueprint_file = getattr(args, "blueprint_file", None)
    skeleton_taskpack_dir = getattr(args, "skeleton_taskpack_dir", None)
    if freeze and dry_run:
        raise AgentTeamCliError("--dry-run and --freeze are mutually exclusive")
    if freeze and not frozen_root:
        raise AgentTeamCliError("--frozen-root is required when --freeze is set")
    if blueprint_file is not None:
        if skeleton_taskpack_dir:
            raise AgentTeamCliError(
                "skeleton_taskpack_dir cannot be used with --blueprint-file"
            )
        return _handle_taskpack_blueprint_materialize(
            args,
            blueprint_file=blueprint_file,
            dry_run=dry_run,
            freeze=freeze,
            frozen_root=frozen_root,
        )
    if not skeleton_taskpack_dir:
        raise AgentTeamCliError(
            "skeleton_taskpack_dir is required with --semantic-json or --semantic-json-file"
        )
    if dry_run:
        raise AgentTeamCliError("--dry-run is supported only with --blueprint-file")
    semantic_task = _load_semantic_task_arg(args.semantic_json, args.semantic_json_file)
    materialized = materialize_semantic_taskpack(
        skeleton_taskpack_dir,
        output_root=args.output_root,
        taskpack_id=args.taskpack_id,
        semantic_task=semantic_task,
    )
    validation = validate_taskpack(materialized["taskpack_dir"])
    frozen = None
    if freeze:
        frozen = freeze_taskpack(materialized["taskpack_dir"], frozen_root)
    summary = {
        "materialize_status": "frozen" if frozen else "draft",
        "taskpack_id": materialized["taskpack_id"],
        "source_taskpack_id": materialized["source_taskpack_id"],
        "materialized": materialized,
        "validation": validation,
        "frozen": frozen,
        "paths": {
            "skeleton_taskpack_dir": str(Path(skeleton_taskpack_dir).resolve()),
            "taskpack_dir": materialized["taskpack_dir"],
            "output_root": str(Path(args.output_root).resolve()),
            "frozen_root": str(Path(frozen_root).resolve()) if frozen_root else None,
        },
    }
    if args.json:
        return summary
    _write_taskpack_materialize_text(summary)
    return 0


def _handle_taskpack_blueprint_materialize(
    args,
    *,
    blueprint_file,
    dry_run,
    freeze,
    frozen_root,
):
    project_root = Path(getattr(args, "project_root", None) or ".").resolve()
    manifest = materialize_taskpack_blueprint(
        project_root,
        blueprint_file,
        args.output_root,
        taskpack_id=args.taskpack_id,
        dry_run=dry_run,
    )
    frozen = None
    if freeze:
        frozen_dir = (Path(frozen_root).resolve() / manifest["taskpack_id"]).resolve()
        frozen_dir_existed = frozen_dir.exists()
        try:
            frozen = freeze_taskpack(manifest["taskpack_dir"], frozen_root)
        except Exception:
            if not frozen_dir_existed and frozen_dir.exists():
                shutil.rmtree(frozen_dir)
            raise
    status = "dry-run" if dry_run else ("frozen" if frozen else "draft")
    frozen_dir = frozen["frozen_taskpack_dir"] if frozen else None
    summary = {
        "materialize_status": status,
        "source_kind": "blueprint",
        "taskpack_id": manifest["taskpack_id"],
        "task_count": manifest["task_count"],
        "dependency_edge_count": manifest["dependency_edge_count"],
        "validation": {"status": manifest["validation_status"]},
        "validation_status": manifest["validation_status"],
        "blueprint_sha256": manifest["blueprint_sha256"],
        "freeze_eligible": manifest["freeze_eligible"],
        "manifest_path": manifest["manifest_path"],
        "taskpack_dir": manifest["taskpack_dir"],
        "frozen_taskpack_dir": frozen_dir,
        "manifest": manifest,
        "frozen": frozen,
        "paths": {
            "project_root": str(project_root),
            "blueprint_file": str(
                (
                    Path(blueprint_file)
                    if Path(blueprint_file).is_absolute()
                    else project_root / blueprint_file
                ).resolve()
            ),
            "output_root": str(Path(args.output_root).resolve()),
            "manifest_path": manifest["manifest_path"],
            "draft_dir": manifest["taskpack_dir"],
            "frozen_root": str(Path(frozen_root).resolve()) if frozen_root else None,
            "frozen_dir": frozen_dir,
        },
    }
    if args.json:
        return summary
    _write_taskpack_materialize_text(summary)
    return 0


def _load_semantic_task_arg(raw_json, json_file):
    if raw_json is not None:
        source = "--semantic-json"
        text = raw_json
    else:
        source = "--semantic-json-file"
        text = Path(json_file).read_text(encoding="utf-8")
    try:
        semantic_task = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentTeamCliError(f"{source} must contain valid JSON", error=str(exc)) from exc
    if not isinstance(semantic_task, dict):
        raise AgentTeamCliError(f"{source} must contain a JSON object")
    return semantic_task


def _write_taskpack_materialize_text(summary):
    if summary.get("source_kind") == "blueprint":
        paths = summary["paths"]
        lines = [
            f"taskpack_id: {summary['taskpack_id']}",
            f"materialize_status: {summary['materialize_status']}",
            f"task_count: {summary['task_count']}",
            f"edge_count: {summary['dependency_edge_count']}",
            f"validation: {summary['validation']['status']}",
            f"blueprint_sha256: {summary['blueprint_sha256']}",
            f"freeze_eligible: {str(summary['freeze_eligible']).lower()}",
            f"manifest_path: {paths['manifest_path'] or '-'}",
            f"draft_dir: {paths['draft_dir'] or '-'}",
        ]
        if paths["frozen_dir"]:
            lines.append(f"frozen_dir: {paths['frozen_dir']}")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        return
    lines = [
        f"taskpack_id: {summary['taskpack_id']}",
        f"materialize_status: {summary['materialize_status']}",
        f"source_taskpack_id: {summary['source_taskpack_id']}",
        f"taskpack_dir: {summary['materialized']['taskpack_dir']}",
        f"validation: {summary['validation']['status']}",
    ]
    frozen = summary.get("frozen")
    if isinstance(frozen, dict):
        lines.append(f"frozen_dir: {frozen['frozen_taskpack_dir']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_taskpack_list(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    if args.work_root:
        profile = {**profile, "work_root": str(Path(args.work_root).resolve())}
    summary = _frozen_taskpack_list_summary(profile)
    if args.json:
        return summary
    _write_taskpack_list_text(summary)
    return 0


def _handle_taskpack_delete(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    if args.work_root:
        profile = {**profile, "work_root": str(Path(args.work_root).resolve())}
    summary = _delete_taskpack_from_profile(
        profile,
        args.taskpack,
        delete_run=args.delete_run,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json:
        return summary
    _write_taskpack_delete_text(summary)
    return 0


def _delete_taskpack_from_profile(profile, taskpack_id, delete_run=False, force=False, dry_run=False):
    work_root = Path(profile["work_root"]).resolve()
    paths = [
        ("draft", _scoped_taskpack_path(work_root, "drafts", taskpack_id)),
        ("frozen", _scoped_taskpack_path(work_root, "frozen", taskpack_id)),
    ]
    run_path = _scoped_taskpack_path(work_root, "runs", taskpack_id)
    skipped_run = None
    if run_path.exists() and not delete_run:
        skipped_run = str(run_path)
        if not dry_run:
            raise AgentTeamCliError(
                "run exists for taskpack; pass --delete-run --force to delete it",
                taskpack_id=taskpack_id,
                run_dir=str(run_path),
            )
    if delete_run:
        paths.append(("run", run_path))
    if not dry_run and not force:
        raise AgentTeamCliError("--force is required for taskpack delete", taskpack_id=taskpack_id)

    candidates = [
        {"kind": kind, "path": str(path), "exists": path.exists()}
        for kind, path in paths
    ]
    if dry_run:
        return {
            "delete_status": "dry_run",
            "taskpack_id": taskpack_id,
            "work_root": str(work_root),
            "candidates": candidates,
            "deleted": [],
            "deleted_count": 0,
            "skipped_run": skipped_run,
        }

    deleted = []
    for kind, path in paths:
        if not path.exists():
            continue
        shutil.rmtree(path)
        deleted.append({"kind": kind, "path": str(path)})
    return {
        "delete_status": "deleted",
        "taskpack_id": taskpack_id,
        "work_root": str(work_root),
        "candidates": candidates,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "skipped_run": skipped_run,
    }


def _scoped_taskpack_path(work_root, section, taskpack_id):
    root = (Path(work_root) / section).resolve()
    path = (root / taskpack_id).resolve()
    if not path.is_relative_to(root):
        raise AgentTeamCliError(
            "taskpack id escapes work root",
            taskpack_id=taskpack_id,
            section=section,
        )
    return path


def _write_taskpack_delete_text(summary):
    lines = [
        f"taskpack: {summary['taskpack_id']}",
        f"delete_status: {summary['delete_status']}",
        f"deleted_count: {summary['deleted_count']}",
    ]
    if summary.get("skipped_run"):
        lines.append(f"skipped_run: {summary['skipped_run']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _projection_output_metadata(payload):
    if not isinstance(payload, dict):
        return {}
    check = payload.get("check") if isinstance(payload.get("check"), dict) else {}

    def value(key):
        return payload.get(key) or check.get(key)

    metadata = {}
    for key in (
        "projection_source",
        "projection_status",
        "projection_warning",
        "next_action",
        "operator_hint",
        "check_status",
    ):
        item = value(key)
        if item:
            metadata[key] = item
    projection_db_path = (
        payload.get("projection_db_path")
        or payload.get("db_path")
        or check.get("projection_db_path")
        or check.get("db_path")
    )
    if projection_db_path:
        metadata["projection_db_path"] = projection_db_path
    return metadata


def _projection_check_metadata(work_root):
    return _projection_output_metadata(check_project_projection_db(work_root))


def _frozen_taskpack_list_summary(profile):
    work_root = Path(profile["work_root"]).resolve()
    frozen_root = work_root / "frozen"
    run_root = work_root / "runs"
    projected = read_projected_taskpacks(work_root, include_fallback_status=True)
    projection_metadata = _projection_output_metadata(projected)
    if projected is not None and projected.get("projection_source") == "db":
        taskpacks = [
            _taskpack_list_item_from_projection(profile, run_root, item)
            for item in projected["taskpacks"]
        ]
        return {
            "project": profile.get("project_key") or "unknown",
            "frozen_root": str(frozen_root),
            "frozen_count": len(taskpacks),
            **projection_metadata,
            "taskpacks": taskpacks,
        }
    if not projection_metadata:
        projection_metadata = _projection_check_metadata(work_root)
    taskpacks = []
    if frozen_root.exists():
        for frozen_dir in sorted(path for path in frozen_root.iterdir() if path.is_dir()):
            taskpack = _read_json_if_exists(frozen_dir / "taskpack.yaml")
            taskpack_id = taskpack.get("taskpack_id") if isinstance(taskpack, dict) else None
            taskpack_id = taskpack_id or frozen_dir.name
            run_dir = run_root / taskpack_id
            item = {
                "taskpack_id": taskpack_id,
                "goal": taskpack.get("goal") if isinstance(taskpack, dict) else None,
                "frozen_dir": str(frozen_dir.resolve()),
                "run_dir": str(run_dir.resolve()) if run_dir.exists() else None,
                "run_status": "not_run",
            }
            if run_dir.exists():
                run_summary = _build_run_status_summary(profile, run_dir)
                item["run_status"] = run_summary.get("liveness_status") or run_summary["status"]
            taskpacks.append(item)
    return {
        "project": profile.get("project_key") or "unknown",
        "frozen_root": str(frozen_root),
        "frozen_count": len(taskpacks),
        **projection_metadata,
        "taskpacks": taskpacks,
    }


def _taskpack_list_item_from_projection(profile, run_root, projected):
    taskpack_id = projected["taskpack_id"]
    run_dir = run_root / taskpack_id
    item = {
        "taskpack_id": taskpack_id,
        "goal": projected.get("goal"),
        "frozen_dir": projected["frozen_dir"],
        "run_dir": str(run_dir.resolve()) if run_dir.exists() else None,
        "run_status": "not_run",
    }
    if run_dir.exists():
        run_summary = _build_run_status_summary(profile, run_dir)
        item["run_status"] = run_summary.get("liveness_status") or run_summary["status"]
    return item


def _write_taskpack_list_text(summary):
    lines = [
        f"project: {summary['project']}",
        f"frozen_count: {summary['frozen_count']}",
        *_projection_text_lines(summary),
    ]
    for item in summary["taskpacks"]:
        details = [
            f"- {item['taskpack_id']}",
            f"run_status={item['run_status']}",
            f"frozen_dir={item['frozen_dir']}",
        ]
        if item.get("run_dir"):
            details.append(f"run_dir={item['run_dir']}")
        lines.append(" ".join(details))
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_init(args):
    project_root = Path(args.project_root or ".").resolve()
    if args.interactive:
        profile = _prompt_project_profile(args, project_root)
    else:
        profile = _profile_from_args(args, project_root)
    profile_path = write_project_profile(project_root, profile, force=args.force)
    summary = {
        "status": "initialized",
        "profile_path": str(profile_path),
        "profile": profile,
    }
    if args.json:
        return summary
    _write_init_text(summary)
    return 0


def _write_init_text(summary):
    profile = summary.get("profile") or {}
    lines = [
        f"project: {profile.get('project_key') or 'unknown'}",
        f"init_status: {summary.get('status') or 'unknown'}",
        f"profile_path: {summary.get('profile_path') or 'unknown'}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_pursue(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    goal = args.goal or _prompt_text("Long-running goal", required=True)
    max_rounds = int(args.max_rounds or 0)
    if max_rounds < 1:
        raise AgentTeamCliError("max rounds must be at least 1", max_rounds=max_rounds)

    result = _run_pursue_loop(
        args,
        project_root=project_root,
        profile=profile,
        goal=goal,
        max_rounds=max_rounds,
    )
    if args.json:
        return result
    _write_pursue_result_text(result)
    return 0


def _run_pursue_loop(args, *, project_root, profile, goal, max_rounds):
    rounds = []
    work_root = Path(args.work_root or profile["work_root"]).resolve()
    current_goal = goal
    source_report = None
    latest_source_report = None
    goal_memory = None
    goal_memory_path = None
    stop_reason = None
    for round_index in range(1, max_rounds + 1):
        initial_integration_base_ref = _pursue_next_integration_base_ref(source_report)
        reusable_repo_map_handoff = _reusable_repo_map_handoff_path(source_report)
        if source_report is not None:
            current_goal = _build_followup_goal(current_goal, source_report, goal_memory=goal_memory)
        taskpack_id = _pursue_taskpack_id(args.taskpack_id, round_index)
        submit_args = _submit_args_from_profile(args, project_root, profile)
        submit_args.goal = current_goal
        submit_args.taskpack_id = taskpack_id
        submit_args.initial_integration_base_ref = initial_integration_base_ref
        submit_args.reuse_repo_map_handoff_path = reusable_repo_map_handoff
        submit_args.progress = not bool(args.json)
        run_result = _handle_submit(submit_args)
        round_record = _pursue_round_record(round_index, run_result, work_root)
        if initial_integration_base_ref:
            round_record["initial_integration_base_ref"] = initial_integration_base_ref
        if reusable_repo_map_handoff:
            round_record["repo_map_handoff_reuse"] = reusable_repo_map_handoff
        rounds.append(round_record)
        stop_reason = _pursue_stop_reason(
            round_record,
            allow_review_gate_follow_up=bool(args.allow_review_gate_follow_up),
        )
        source_run_dir = Path(round_record["run_dir"])
        latest_source_report = build_run_completion_report(
            source_run_dir,
            project=profile.get("project_key") or "agentteam",
            write_files=False,
        )
        goal_memory = build_goal_memory(
            pursue_id=_pursue_id(args, rounds),
            original_goal=goal,
            work_root=work_root,
            rounds=rounds,
            source_report=latest_source_report,
            stop_reason=stop_reason,
            previous_memory=goal_memory,
        )
        goal_memory_path = write_goal_memory(work_root, goal_memory)
        goal_memory["memory_path"] = str(goal_memory_path)
        round_record["goal_memory_path"] = str(goal_memory_path)
        if stop_reason:
            break
        queue_summary = _pursue_follow_up_queue_summary(
            source_report=latest_source_report,
            goal_memory=goal_memory,
            source_run_dir=source_run_dir,
        )
        round_record["follow_up_queue"] = _compact_pursue_queue_summary(queue_summary)
        if queue_summary.get("next_goal"):
            round_record["selected_next_goal"] = queue_summary["next_goal"]
        if queue_summary.get("queue_status") != "ready":
            stop_reason = "follow_up_queue_empty"
            goal_memory = build_goal_memory(
                pursue_id=_pursue_id(args, rounds),
                original_goal=goal,
                work_root=work_root,
                rounds=rounds,
                source_report=latest_source_report,
                stop_reason=stop_reason,
                previous_memory=goal_memory,
            )
            goal_memory_path = write_goal_memory(work_root, goal_memory)
            goal_memory["memory_path"] = str(goal_memory_path)
            round_record["goal_memory_path"] = str(goal_memory_path)
            break
        source_report = latest_source_report
        current_goal = queue_summary["next_goal"]
    if stop_reason is None:
        stop_reason = "max_rounds_reached"
        if rounds:
            goal_memory = build_goal_memory(
                pursue_id=_pursue_id(args, rounds),
                original_goal=goal,
                work_root=work_root,
                rounds=rounds,
                source_report=latest_source_report,
                stop_reason=stop_reason,
                previous_memory=goal_memory,
            )
            goal_memory_path = write_goal_memory(work_root, goal_memory)
            goal_memory["memory_path"] = str(goal_memory_path)
            rounds[-1]["goal_memory_path"] = str(goal_memory_path)
    pursue_id = _pursue_id(args, rounds)
    recap = _build_pursue_recap(
        pursue_id=pursue_id,
        goal=goal,
        max_rounds=max_rounds,
        rounds=rounds,
        stop_reason=stop_reason,
        work_root=work_root,
        goal_memory_path=goal_memory_path,
    )
    recap_path = _write_pursue_recap(work_root, recap)
    return {
        "pursue_status": "stopped",
        "pursue_id": pursue_id,
        "goal": goal,
        "max_rounds": max_rounds,
        "rounds_completed": len(rounds),
        "stop_reason": stop_reason,
        "runs": rounds,
        "work_root": str(work_root),
        "pursue_recap_path": str(recap_path),
        "goal_memory_path": str(goal_memory_path) if goal_memory_path else None,
        "operator_next_action": recap.get("operator_next_action"),
    }


def _pursue_id(args, rounds):
    if getattr(args, "taskpack_id", None):
        return args.taskpack_id
    if rounds and rounds[0].get("taskpack_id"):
        return rounds[0]["taskpack_id"]
    return f"pursue-{int(time.time())}"


def _build_pursue_recap(*, pursue_id, goal, max_rounds, rounds, stop_reason, work_root, goal_memory_path=None):
    latest = rounds[-1] if rounds else {}
    latest_taskpack_id = latest.get("taskpack_id")
    latest_report_path = latest.get("report_path")
    recap = {
        "pursue_id": pursue_id,
        "pursue_status": "stopped",
        "goal": goal,
        "work_root": str(work_root),
        "rounds_completed": len(rounds),
        "max_rounds": max_rounds,
        "stop_reason": stop_reason,
        "latest_taskpack_id": latest_taskpack_id,
        "latest_report_path": latest_report_path,
        "goal_memory_path": str(goal_memory_path) if goal_memory_path else None,
        "operator_next_action": _pursue_operator_next_action(stop_reason, latest),
        "runs": rounds,
        "updated_at": _format_utc_timestamp(datetime.now(UTC)),
    }
    if isinstance(latest.get("follow_up_queue"), dict):
        recap["latest_follow_up_queue"] = _compact_pursue_queue_summary(latest["follow_up_queue"])
    if latest.get("selected_next_goal"):
        recap["latest_selected_next_goal"] = latest["selected_next_goal"]
    return recap


def _write_pursue_recap(work_root, recap):
    pursue_id = recap.get("pursue_id") or "pursue"
    recap_root = Path(work_root) / "pursue"
    recap_root.mkdir(parents=True, exist_ok=True)
    recap_path = recap_root / f"{_safe_pursue_artifact_id(pursue_id)}.json"
    recap = dict(recap)
    recap["recap_path"] = str(recap_path.resolve())
    _write_json(recap_path, recap)
    return recap_path.resolve()


def _safe_pursue_artifact_id(value):
    safe = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in str(value)
    ).strip(".-")
    return safe or "pursue"


def _pursue_operator_next_action(stop_reason, latest):
    taskpack_id = latest.get("taskpack_id") if isinstance(latest, dict) else None
    run_dir = latest.get("run_dir") if isinstance(latest, dict) else None
    if stop_reason == "max_rounds_reached" and taskpack_id:
        return f"agentteam queue next --taskpack {taskpack_id}"
    if taskpack_id:
        return f"agentteam report --taskpack {taskpack_id}"
    if run_dir:
        return f"agentteam report --run-dir {run_dir}"
    return "agentteam status"


def _pursue_taskpack_id(base_taskpack_id, round_index):
    if not base_taskpack_id:
        return None
    if round_index == 1:
        return base_taskpack_id
    return f"{base_taskpack_id}-r{round_index}"


def _pursue_round_record(round_index, run_result, work_root):
    taskpack_id = run_result.get("taskpack_id") or "unknown"
    report = run_result.get("report") if isinstance(run_result.get("report"), dict) else {}
    run_dir = Path(work_root) / "runs" / taskpack_id
    return {
        "round": round_index,
        "taskpack_id": taskpack_id,
        "status": run_result.get("status") or "unknown",
        "run_status": report.get("run_status"),
        "blocked_count": report.get("blocked_count", 0),
        "report_path": report.get("report_path"),
        "run_dir": str(run_dir.resolve()),
        "follow_up_recommendation": (
            report.get("completion_summary", {}).get("follow_up_recommendation")
            if isinstance(report.get("completion_summary"), dict)
            else None
        ),
    }


def _pursue_stop_reason(round_record, *, allow_review_gate_follow_up=False):
    status = round_record.get("status")
    run_status = round_record.get("run_status")
    if status == "stopped" or run_status == "stopped":
        return "stopped"
    if status in {"manual_gate_required", "permission_request_required", "blocked", "failed"}:
        return status
    if int(round_record.get("blocked_count") or 0) > 0:
        return "blocked"
    recommendation = round_record.get("follow_up_recommendation")
    action = recommendation.get("action") if isinstance(recommendation, dict) else None
    if action in {"integrate", "integrate_then_next"} and not allow_review_gate_follow_up:
        return "review_gate_required"
    return None


def _pursue_next_goal(original_goal, source_report):
    summary = source_report.get("completion_summary") if isinstance(source_report, dict) else {}
    next_step = _first_non_empty_text(summary.get("next_steps") if isinstance(summary, dict) else None)
    if next_step:
        return next_step
    return f"Continue pursuing the long-running goal using the previous report: {original_goal}"


def _pursue_next_integration_base_ref(source_report):
    if not isinstance(source_report, dict):
        return None
    baseline = source_report.get("integration_baseline")
    if not isinstance(baseline, dict):
        return None
    for key in ("head_sha", "branch"):
        value = baseline.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _pursue_follow_up_queue_summary(*, source_report, goal_memory=None, source_run_dir=None, limit=5):
    source_report = source_report if isinstance(source_report, dict) else {}
    source_run_dir_text = str(source_run_dir) if source_run_dir else source_report.get("run_dir")
    source_taskpack_id = source_report.get("run_id")
    if not source_taskpack_id and source_run_dir:
        source_taskpack_id = Path(source_run_dir).name
    return build_follow_up_queue_summary(
        source_report=source_report,
        goal_memory=goal_memory if isinstance(goal_memory, dict) else {},
        source_taskpack_id=source_taskpack_id,
        source_run_dir=source_run_dir_text,
        limit=limit,
    )


def _compact_pursue_queue_summary(summary):
    summary = summary if isinstance(summary, dict) else {}
    compact = {
        "queue_status": summary.get("queue_status") or "unknown",
        "source_taskpack_id": summary.get("source_taskpack_id"),
        "item_count": summary.get("item_count", 0),
        "next_goal": summary.get("next_goal"),
        "next_command": summary.get("next_command"),
    }
    selected_item = summary.get("selected_item")
    if isinstance(selected_item, dict):
        compact["selected_item"] = {
            "objective": selected_item.get("objective"),
            "source": selected_item.get("source"),
            "source_taskpack_id": selected_item.get("source_taskpack_id"),
            "source_report_path": selected_item.get("source_report_path"),
        }
    if summary.get("operator_hint"):
        compact["operator_hint"] = summary["operator_hint"]
    return compact


def _write_pursue_result_text(result):
    latest = result.get("runs", [])[-1] if result.get("runs") else {}
    lines = [
        f"pursue_status: {result.get('pursue_status') or 'unknown'}",
        f"rounds_completed: {result.get('rounds_completed', 0)}/{result.get('max_rounds', 0)}",
        f"stop_reason: {result.get('stop_reason') or 'unknown'}",
    ]
    if latest:
        lines.append(f"latest_taskpack_id: {latest.get('taskpack_id') or 'unknown'}")
        if latest.get("report_path"):
            lines.append(f"latest_report: {latest['report_path']}")
    if result.get("pursue_recap_path"):
        lines.append(f"pursue_recap: {result['pursue_recap_path']}")
    if result.get("operator_next_action"):
        lines.append(f"operator_next_action: {result['operator_next_action']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _handle_start(args):
    project_root = Path(args.project_root or ".").resolve()
    profile_path = profile_path_for_project(project_root)
    try:
        profile = load_project_profile(project_root)
    except AgentTeamProfileError as exc:
        if not _prompt_bool(f"Create AgentTeam profile at {profile_path}", default=True):
            raise AgentTeamCliError(str(exc), profile_path=str(profile_path)) from exc
        profile = _prompt_project_profile(args, project_root)
        profile_path = write_project_profile(project_root, profile, force=False)

    _write_progress(f"profile loaded: {profile.get('project_key') or project_root.name}")
    goal = args.goal or _prompt_text("Goal", required=True)
    submit_args = _submit_args_from_profile(args, project_root, profile)
    submit_args.goal = goal
    submit_args.progress = True
    result = _handle_submit(submit_args)
    result["profile"] = {
        "profile_path": str(profile_path.resolve()),
        "project_key": profile.get("project_key"),
    }
    if args.json:
        return result
    _write_execution_result_text(result)
    return 0


def _handle_next(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(args.work_root or profile["work_root"]).resolve()
    source_run_dir = _followup_source_run_dir(args, profile, work_root)
    if not source_run_dir.exists():
        raise AgentTeamCliError("source run not found", run_dir=str(source_run_dir))
    source_report = build_run_completion_report(
        source_run_dir,
        project=profile.get("project_key") or "agentteam",
    )
    requested_goal = args.goal or _prompt_text("Follow-up goal", required=True)
    followup_goal = _build_followup_goal(requested_goal, source_report)
    reusable_repo_map_handoff = _reusable_repo_map_handoff_path(source_report)

    _write_progress(f"profile loaded: {profile.get('project_key') or project_root.name}")
    _write_progress(f"follow-up source: {source_run_dir.name}")
    submit_args = _submit_args_from_profile(args, project_root, profile)
    submit_args.goal = followup_goal
    submit_args.progress = True
    submit_args.reuse_repo_map_handoff_path = reusable_repo_map_handoff
    result = _handle_submit(submit_args)
    result["follow_up"] = {
        "source_taskpack_id": source_run_dir.name,
        "source_run_dir": str(source_run_dir),
        "source_report_path": source_report["report_path"],
        "requested_goal": requested_goal,
        "repo_map_handoff_reuse": reusable_repo_map_handoff,
    }
    if args.json:
        return result
    _write_execution_result_text(result)
    return 0


def _handle_queue(args):
    project_root = Path(args.project_root or ".").resolve()
    profile, work_root = _queue_profile_and_work_root(args, project_root)
    run_dir = _queue_source_run_dir(args, profile, work_root)
    if not run_dir.exists():
        raise AgentTeamCliError("source run not found", run_dir=str(run_dir))
    source_report = build_run_completion_report(
        run_dir,
        project=profile.get("project_key") or "agentteam",
    )
    goal_memory = _latest_goal_memory_for_run(work_root, run_dir, source_report)
    summary = build_follow_up_queue_summary(
        source_report=source_report,
        goal_memory=goal_memory,
        source_taskpack_id=run_dir.name,
        source_run_dir=str(run_dir),
        limit=args.limit,
    )
    if args.json:
        return summary
    sys.stdout.write(render_follow_up_queue_text(summary, next_only=args.queue_command == "next"))
    sys.stdout.flush()
    return 0


def _queue_profile_and_work_root(args, project_root):
    return _profile_and_work_root_for_args_run_dir(args, project_root)


def _profile_and_work_root_for_args_run_dir(args, project_root):
    if getattr(args, "run_dir", None):
        run_dir = Path(args.run_dir).resolve()
        work_root = _infer_work_root_from_run_dir(run_dir).resolve()
        profile_path = profile_path_for_project(project_root)
        if profile_path.exists():
            profile = load_project_profile(project_root)
            profile = {**profile, "work_root": str(work_root)}
        else:
            profile = {"project_key": "unknown", "work_root": str(work_root)}
        return profile, work_root
    profile = load_project_profile(project_root)
    return profile, Path(profile["work_root"]).resolve()


def _infer_work_root_from_run_dir(run_dir):
    run_dir = Path(run_dir).resolve()
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent
    if run_dir.parent.parent.name == "runs" and run_dir.parent.name == run_dir.name:
        return run_dir.parent.parent.parent
    return run_dir.parent


def _queue_source_run_dir(args, profile, work_root):
    if args.run_dir:
        return Path(args.run_dir).resolve()
    if args.taskpack:
        return (work_root / "runs" / args.taskpack).resolve()
    return _latest_run_dir(profile)


def _latest_goal_memory_for_run(work_root, run_dir, source_report):
    pursue_recap = source_report.get("pursue_recap") if isinstance(source_report, dict) else {}
    if isinstance(pursue_recap, dict) and pursue_recap.get("goal_memory_path"):
        memory = _read_json_if_exists(pursue_recap["goal_memory_path"])
        if memory:
            return memory
    memory = _read_json_if_exists(goal_memory_path(work_root, run_dir.name))
    if memory:
        return memory
    pursue_root = Path(work_root) / "pursue"
    if not pursue_root.exists():
        return {}
    candidates = []
    for path in pursue_root.glob("*-goal-memory.json"):
        memory = _read_json_if_exists(path)
        if not isinstance(memory, dict):
            continue
        if memory.get("latest_taskpack_id") == run_dir.name or run_dir.name in (memory.get("latest_run_ids") or []):
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0
            candidates.append((modified, memory))
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def _handle_feedback(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    if args.feedback_command == "propose":
        run_dir = _queue_source_run_dir(args, profile, work_root)
        if not run_dir.exists():
            raise AgentTeamCliError("source run not found", run_dir=str(run_dir))
        source_report = build_run_completion_report(
            run_dir,
            project=profile.get("project_key") or "agentteam",
        )
        proposal = write_semantic_feedback_proposal(
            work_root=work_root,
            proposal_id=args.proposal_id,
            source_report=source_report,
            target_artifacts=args.target_artifact,
            summary=args.summary,
            rationale=args.rationale,
        )
        if args.json:
            return proposal
        _write_feedback_proposal_text(proposal)
        return 0
    if args.feedback_command == "list":
        summary = list_semantic_feedback_proposals(work_root)
        if args.json:
            return summary
        sys.stdout.write(render_semantic_feedback_text(summary))
        sys.stdout.flush()
        return 0
    raise AgentTeamCliError("unknown feedback command", command=args.feedback_command)


def _write_feedback_proposal_text(proposal):
    lines = [
        f"proposal_status: {proposal.get('proposal_status') or 'unknown'}",
        f"proposal_id: {proposal.get('proposal_id') or 'unknown'}",
        f"source_taskpack_id: {proposal.get('source_taskpack_id') or 'unknown'}",
        f"proposal_path: {proposal.get('proposal_path') or 'unknown'}",
    ]
    if proposal.get("summary"):
        lines.append(f"summary: {proposal['summary']}")
    lines.append("authority_boundary: proposal_only")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _followup_source_run_dir(args, profile, work_root):
    if args.from_run_dir:
        return Path(args.from_run_dir).resolve()
    if args.from_taskpack:
        return (work_root / "runs" / args.from_taskpack).resolve()
    return _latest_run_dir(profile)


def _build_followup_goal(requested_goal, source_report, goal_memory=None):
    source_taskpack_id = source_report.get("run_id") or "unknown"
    report_path = source_report.get("report_path") or "unknown"
    run_dir = source_report.get("run_dir") or "unknown"
    lines = [
        "Follow-up goal:",
        str(requested_goal),
        "",
        "Previous taskpack context:",
        f"- source_taskpack_id: {source_taskpack_id}",
        f"- source_run_dir: {run_dir}",
        f"- source_report_path: {report_path}",
        f"- source_run_status: {source_report.get('run_status') or 'unknown'}",
        f"- source_scheduler_status: {source_report.get('scheduler_status') or 'unknown'}",
        (
            "- source_summary: "
            f"tasks={source_report.get('task_count', 0)} "
            f"blocked={source_report.get('blocked_count', 0)}"
        ),
        "",
        "Instructions for the new taskpack:",
        "- Treat the previous taskpack as immutable history; do not rewrite its run artifacts.",
        "- Read the source report before drafting or executing the follow-up work.",
        "- Use the previous findings, verification results, blockers, and next steps as context.",
    ]
    task_lines = _followup_task_summary_lines(source_report)
    if task_lines:
        lines.extend(["", "Previous task summaries:", *task_lines])
    memory_context = render_goal_memory_prompt_context(goal_memory)
    if memory_context:
        lines.extend(["", memory_context])
    return "\n".join(lines)


def _reusable_repo_map_handoff_path(source_report):
    if not isinstance(source_report, dict):
        return None
    baseline = source_report.get("integration_baseline")
    if not isinstance(baseline, dict):
        return None
    if baseline.get("worktree_exists") is False:
        return None
    worktree_path = baseline.get("worktree_path")
    if not isinstance(worktree_path, str) or not worktree_path.strip():
        return None
    handoff_path = Path(worktree_path) / REPO_MAP_HANDOFF_PATH
    if not handoff_path.is_file():
        return None
    try:
        payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return REPO_MAP_HANDOFF_PATH


def _followup_task_summary_lines(source_report, limit=3):
    operator_report = source_report.get("operator_report")
    if not isinstance(operator_report, dict):
        return []
    task_reports = operator_report.get("task_reports")
    if not isinstance(task_reports, list):
        return []
    lines = []
    for task in task_reports[:limit]:
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or "unknown"
        status = task.get("status") or "unknown"
        changed = _first_non_empty_text(task.get("what_changed"))
        next_step = _first_non_empty_text(task.get("next_steps"))
        line = f"- {task_id}: status={status}"
        if changed:
            line += f"; changed={changed}"
        if next_step:
            line += f"; next={next_step}"
        lines.append(line)
    return lines


def _first_non_empty_text(value):
    if isinstance(value, list):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _handle_submit(args):
    _complete_submit_args(args)
    work_root = Path(args.work_root).resolve()
    draft_root = work_root / "drafts"
    frozen_root = work_root / "frozen"
    run_root = work_root / "runs"
    runtime_backend = _submit_runtime_backend(args.runtime, args.author_runtime)
    progress = bool(getattr(args, "progress", False))

    _progress(progress, f"authoring taskpack with {args.author_runtime}")
    draft = draft_taskpack_from_goal(
        project_root=args.project_root,
        goal=args.goal,
        draft_root=draft_root,
        author_runtime=args.author_runtime,
        taskpack_id=args.taskpack_id,
        codex_command=args.codex_command,
        codex_timeout_seconds=args.codex_timeout_seconds,
        verification_profile=getattr(args, "verification_profile", None),
        progress_callback=_author_progress_callback(progress),
    )
    _progress(progress, f"draft accepted: {draft['taskpack_id']}")
    taskpack_dir = Path(draft["taskpack_dir"])
    _set_taskpack_runtime_backend(taskpack_dir, runtime_backend)
    _set_taskpack_codex_model(
        taskpack_dir,
        getattr(args, "codex_model", None),
        runtime_backend=runtime_backend,
    )
    repo_map_handoff_reuse = None
    reuse_handoff_path = getattr(args, "reuse_repo_map_handoff_path", None)
    if reuse_handoff_path:
        repo_map_handoff_reuse = reuse_repo_map_handoff_in_taskpack(
            taskpack_dir,
            reuse_handoff_path,
        )
        _progress(
            progress,
            "repo_map_handoff reuse "
            f"{repo_map_handoff_reuse['status']}: {repo_map_handoff_reuse['handoff_path']}",
        )
    validation = validate_taskpack(taskpack_dir)
    frozen = freeze_taskpack(taskpack_dir, frozen_root)
    _progress(progress, f"frozen taskpack created: {frozen['manifest']['taskpack_id']}")
    _progress(progress, f"runtime started: {run_root / frozen['manifest']['taskpack_id']}")
    completed = _run_frozen_taskpack(
        frozen["frozen_taskpack_dir"],
        run_root=run_root,
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
        notification_project=args.notification_project,
        feishu_webhook_env=args.feishu_webhook_env,
        feishu_signing_secret_env=args.feishu_signing_secret_env,
        progress=progress,
        initial_integration_base_ref=getattr(args, "initial_integration_base_ref", None),
    )
    if completed.returncode != 0:
        raise AgentTeamCliError(
            "agentteam submit run step failed",
            step="run",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    run = _json_or_output(completed.stdout)
    run_dir = run_root / frozen["manifest"]["taskpack_id"]
    release_record = _record_run_release(
        run_dir,
        {"work_root": str(work_root)},
    )
    report = _build_gate_guided_completion_report(
        run_dir,
        project=args.notification_project or "agentteam",
        profile={"work_root": str(work_root)},
    )
    artifact_snapshot = snapshot_run_artifacts_safe(
        work_root,
        run_dir,
        taskpack_id=frozen["manifest"]["taskpack_id"],
        project=args.notification_project or "agentteam",
    )
    _progress(progress, _artifact_snapshot_progress(artifact_snapshot))
    _progress_completion_report(progress, report)
    _progress(progress, f"run {_run_progress_status(run)}")
    result = {
        "status": _submit_status_from_run(run),
        "taskpack_id": draft["taskpack_id"],
        "runtime": runtime_backend,
        "runtime_release": release_record,
        "draft": draft,
        "validation": validation,
        "freeze": frozen,
        "run": run,
        "report": {
            "report_path": report["report_path"],
            "report_json_path": report["report_json_path"],
            "run_status": report["run_status"],
            "task_count": report["task_count"],
            "blocked_count": report["blocked_count"],
            "token_usage": report["token_usage"],
            "completion_summary": report["completion_summary"],
        },
        "artifact_snapshot": artifact_snapshot,
        "paths": {
            "work_root": str(work_root),
            "draft_root": str(draft_root),
            "frozen_root": str(frozen_root),
            "run_root": str(run_root),
        },
    }
    if repo_map_handoff_reuse is not None:
        result["repo_map_handoff_reuse"] = repo_map_handoff_reuse
    return result


def _handle_run(args):
    run_paths = _run_paths_for_frozen_taskpack(args.frozen_taskpack_dir, args.run_root)
    completed = _run_frozen_taskpack(
        args.frozen_taskpack_dir,
        run_root=args.run_root,
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
        notification_project=args.notification_project,
        feishu_webhook_env=args.feishu_webhook_env,
        feishu_signing_secret_env=args.feishu_signing_secret_env,
    )
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()
    if completed.returncode != 0:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
            sys.stdout.flush()
        return completed.returncode
    run = _json_or_output(completed.stdout)
    run_dir = run_paths["run_dir"]
    work_root = _infer_work_root_for_run(run_paths["run_root"], args.frozen_taskpack_dir)
    report = _build_gate_guided_completion_report(
        run_dir,
        project=args.notification_project or "agentteam",
        profile={"work_root": str(work_root)},
    )
    artifact_snapshot = snapshot_run_artifacts_safe(
        work_root,
        run_dir,
        taskpack_id=run_paths["taskpack_id"],
        project=args.notification_project or "agentteam",
    )
    result = {
        "status": _submit_status_from_run(run),
        "taskpack_id": run_paths["taskpack_id"],
        "run": run,
        "report": {
            "report_path": report["report_path"],
            "report_json_path": report["report_json_path"],
            "run_status": report["run_status"],
            "task_count": report["task_count"],
            "blocked_count": report["blocked_count"],
            "token_usage": report["token_usage"],
            "completion_summary": report["completion_summary"],
        },
        "artifact_snapshot": artifact_snapshot,
        "paths": {
            "work_root": str(work_root),
            "run_root": str(run_paths["run_root"]),
            "run_dir": str(run_dir),
            "frozen_taskpack_dir": str(Path(args.frozen_taskpack_dir).resolve()),
        },
    }
    if args.json:
        return result
    _write_execution_result_text(result)
    return 0


def _infer_work_root_for_run(run_root, frozen_taskpack_dir):
    run_root = Path(run_root).resolve()
    frozen_taskpack_dir = Path(frozen_taskpack_dir).resolve()
    if run_root.name == "runs":
        return run_root.parent.resolve()
    if frozen_taskpack_dir.parent.name == "frozen":
        return frozen_taskpack_dir.parent.parent.resolve()
    return run_root.parent.resolve()


def _handle_continue(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    taskpack_id = _continue_taskpack_id(args, profile)
    frozen_dir = (work_root / "frozen" / taskpack_id).resolve()
    run_dir = Path(args.run_dir).resolve() if args.run_dir else (work_root / "runs" / taskpack_id).resolve()
    run_root = run_dir.parent.resolve()
    _require_existing_frozen_and_run(taskpack_id, frozen_dir, run_dir)
    selection = _launcher_runtime_selection()
    if selection and selection.get("selection_mode") == "implicit_latest":
        state = _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json")
        if not state:
            state = _read_json_if_exists(run_dir / "state" / "scheduler_state.json")
        scheduler_status = state.get("scheduler_status") if isinstance(state, dict) else None
        if scheduler_status in {"failed", "cancelled", "canceled", "stopped"}:
            raise AgentTeamCliError(
                "latest implementation run is not resumable; select an older run explicitly",
                taskpack_id=taskpack_id,
                scheduler_status=scheduler_status,
                selection_mode="implicit_latest",
            )

    _write_progress(f"profile loaded: {profile.get('project_key') or project_root.name}")
    _write_progress(f"continuing taskpack: {taskpack_id}")
    lease_refresh = _refresh_inflight_leases(run_dir)
    completed = _run_frozen_taskpack(
        frozen_dir,
        run_root=run_root,
        one_shot=_override_or_profile(args.one_shot, profile.get("one_shot", False)),
        max_inflight=args.max_inflight or profile.get("max_inflight", 2),
        max_attempts=args.max_attempts or profile.get("max_attempts", 1),
        commit_verified_integration=_override_or_profile(
            args.commit_verified_integration,
            profile.get("commit_verified_integration", False),
        ),
        notification_project=args.notification_project
        or profile.get("notification_project")
        or profile.get("project_key")
        or "agentteam",
        feishu_webhook_env=_profile_feishu_value(args, profile, "webhook_env"),
        feishu_signing_secret_env=_profile_feishu_value(args, profile, "signing_secret_env"),
        progress=True,
    )
    if completed.returncode != 0:
        raise AgentTeamCliError(
            "agentteam continue run step failed",
            step="run",
            taskpack_id=taskpack_id,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    run = _json_or_output(completed.stdout)
    release_record = _record_run_release(run_dir, profile)
    report = _build_gate_guided_completion_report(
        run_dir,
        project=profile.get("project_key") or "agentteam",
        profile=profile,
    )
    artifact_snapshot = snapshot_run_artifacts_safe(
        work_root,
        run_dir,
        taskpack_id=taskpack_id,
        project=profile.get("project_key") or "agentteam",
    )
    _write_progress(_artifact_snapshot_progress(artifact_snapshot))
    _progress_completion_report(True, report)
    _write_progress(f"run {_run_progress_status(run)}")
    result = {
        "continue_status": "continued",
        "status": _submit_status_from_run(run),
        "taskpack_id": taskpack_id,
        "lease_refresh": lease_refresh,
        "runtime_release": release_record,
        "run": run,
        "report": {
            "report_path": report["report_path"],
            "report_json_path": report["report_json_path"],
            "run_status": report["run_status"],
            "task_count": report["task_count"],
            "blocked_count": report["blocked_count"],
            "token_usage": report["token_usage"],
            "completion_summary": report["completion_summary"],
        },
        "artifact_snapshot": artifact_snapshot,
        "paths": {
            "work_root": str(work_root),
            "frozen_taskpack_dir": str(frozen_dir),
            "run_dir": str(run_dir),
        },
    }
    if args.json:
        return result
    _write_execution_result_text(result)
    return 0


def _continue_taskpack_id(args, profile):
    if args.taskpack:
        taskpack_id = args.taskpack
    elif args.run_dir:
        taskpack_id = Path(args.run_dir).resolve().name
    else:
        if not _launcher_runtime_selection():
            raise AgentTeamCliError(
                "direct runtime execution cannot select the latest run implicitly; "
                "use the agentteam launcher or provide --taskpack/--run-dir"
            )
        taskpack_id = _latest_run_dir(profile).name
    if not taskpack_id:
        raise AgentTeamCliError("taskpack id is required for continue")
    if args.run_dir and Path(args.run_dir).resolve().name != taskpack_id:
        raise AgentTeamCliError(
            "run directory name must match taskpack id",
            taskpack_id=taskpack_id,
            run_dir=str(Path(args.run_dir).resolve()),
        )
    return taskpack_id


def _require_existing_frozen_and_run(taskpack_id, frozen_dir, run_dir):
    if not frozen_dir.exists():
        raise AgentTeamCliError(
            "frozen taskpack not found",
            taskpack_id=taskpack_id,
            frozen_taskpack_dir=str(frozen_dir),
        )
    if not (frozen_dir / "taskpack.yaml").exists():
        raise AgentTeamCliError(
            "frozen taskpack is missing taskpack.yaml",
            taskpack_id=taskpack_id,
            frozen_taskpack_dir=str(frozen_dir),
        )
    if not run_dir.exists():
        raise AgentTeamCliError(
            "run not found for frozen taskpack",
            taskpack_id=taskpack_id,
            run_dir=str(run_dir),
        )


def _refresh_inflight_leases(run_dir):
    state_path = Path(run_dir) / "state" / "two_phase_scheduler_state.json"
    state = _read_json_if_exists(state_path)
    attempts = state.get("inflight_attempts") if isinstance(state, dict) else None
    if not isinstance(attempts, list) or not attempts:
        return {"refreshed_count": 0}
    lease_timeout = state.get("lease_timeout_seconds", 3600)
    if not isinstance(lease_timeout, int) or lease_timeout < 0:
        lease_timeout = 3600
    expires_at = _format_utc_timestamp(datetime.now(UTC) + timedelta(seconds=max(lease_timeout, 60)))
    refreshed_count = 0
    for attempt in attempts:
        if isinstance(attempt, dict):
            attempt["lease_expires_at"] = expires_at
            refreshed_count += 1
    if refreshed_count:
        state["scheduler_status"] = "running"
        _write_json(state_path, state)
    return {"refreshed_count": refreshed_count, "lease_expires_at": expires_at}


def _profile_feishu_value(args, profile, field):
    arg_name = f"feishu_{field}"
    arg_value = getattr(args, arg_name, None)
    if arg_value is not None:
        return arg_value
    feishu = profile.get("feishu") if isinstance(profile.get("feishu"), dict) else {}
    if not feishu.get("enabled"):
        return None
    return feishu.get(field)


def _format_utc_timestamp(timestamp):
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _record_run_stale_detected_if_needed(run_dir, summary):
    if summary.get("liveness_status") != "running-stale":
        return None
    return _append_run_event_once(
        run_dir,
        "run_stale_detected",
        {
            "run_id": Path(run_dir).resolve().name,
            "run_status": summary.get("run_status") or summary.get("status"),
            "overall_status": summary.get("overall_status"),
            "liveness_status": summary.get("liveness_status"),
            "processes": summary.get("processes"),
            "workers": summary.get("workers"),
            "runtime_release": summary.get("runtime_release"),
        },
    )


def _append_run_event_once(run_dir, event_type, payload):
    run_dir = Path(run_dir).resolve()
    events_path = run_dir / "events.jsonl"
    events = _read_jsonl(events_path)
    for event in events:
        if event.get("event_type") == event_type:
            return event
    sequence = max(
        [
            int(event.get("sequence", 0))
            for event in events
            if isinstance(event, dict) and str(event.get("sequence", "")).isdigit()
        ],
        default=0,
    ) + 1
    event = {
        "event_id": f"EVT-{sequence:04d}",
        "sequence": sequence,
        "time": _format_utc_timestamp(datetime.now(UTC)),
        "event_type": event_type,
        "actor": "agentteam-cli",
        "target_agent_id": None,
        "idempotency_key": f"{event_type}:{run_dir.name}",
        "correlation_id": f"run:{run_dir.name}",
        "run_id": run_dir.name,
        "step_id": "STEP-RUN",
        "payload": payload,
    }
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def _handle_status(args):
    project_root = Path(args.project_root or ".").resolve()
    profile, _work_root = _profile_and_work_root_for_args_run_dir(args, project_root)
    run_dir = None
    try:
        run_dir = _canonical_run_dir(Path(args.run_dir).resolve()) if args.run_dir else _latest_run_dir(profile)
        summary = _build_run_status_summary(profile, run_dir)
    except AgentTeamCliError as exc:
        authoring = _build_project_authoring_summary(profile)
        if not args.run_dir and authoring["active_count"]:
            summary = _build_project_status_summary(profile, authoring)
        else:
            raise exc
    if run_dir is not None and summary.get("status_scope") != "project":
        stale_event = _record_run_stale_detected_if_needed(run_dir, summary)
        if stale_event:
            summary["stale_event"] = stale_event
    if args.json:
        return summary
    if summary.get("status_scope") == "project":
        _write_project_status_text(summary)
    else:
        _write_status_text(summary)
    return 0


def _handle_paths(args):
    profile = _watch_profile(args)
    run_dir = _watch_run_dir(args, profile)
    summary = _build_paths_summary(args, profile, run_dir)
    if args.json:
        return summary
    _write_paths_text(summary)
    return 0


def _handle_integrate(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    run_dir = _selected_run_dir(args, profile, command_name="integrate")
    summary = _integrate_run_baseline(
        project_root,
        profile,
        run_dir,
        rebase=args.rebase,
        record_only=args.record_only,
    )
    if args.json:
        return summary
    _write_integrate_text(summary)
    return 0


def _handle_gate(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    run_dir = _selected_run_dir(args, profile, command_name=f"gate {args.gate_command}")
    if args.gate_command == "seal-baseline":
        summary = _gate_seal_baseline(
            project_root,
            profile,
            run_dir,
            expected_integration_head=args.expected_integration_head,
        )
    elif args.gate_command == "refresh-baseline":
        summary = _gate_refresh_baseline(
            project_root,
            profile,
            run_dir,
            expected_gate_epoch=args.expected_gate_epoch,
            expected_target_head=args.expected_target_head,
        )
    elif args.gate_command == "register":
        summary = _gate_register(
            project_root,
            profile,
            run_dir,
            gate_id=args.gate,
            gate_epoch=args.gate_epoch,
            evidence_run_id=args.evidence_run,
            expected_integration_head=args.expected_integration_head,
        )
    elif args.gate_command == "approve":
        summary = _gate_approve(
            project_root,
            profile,
            run_dir,
            gate_id=args.gate,
            gate_epoch=args.gate_epoch,
            expected_evidence_sha256=args.expected_evidence_sha256,
            expected_integration_head=args.expected_integration_head,
        )
    else:
        raise AgentTeamCliError("unsupported gate command", gate_command=args.gate_command)
    if args.json:
        return summary
    _write_gate_text(summary)
    return 0


def _write_gate_text(summary):
    lines = [
        f"gate_action: {summary.get('gate_action') or 'unknown'}",
        f"gate_status: {summary.get('gate_status') or 'unknown'}",
        f"taskpack_id: {summary.get('taskpack_id') or 'unknown'}",
        f"gate_epoch: {summary.get('gate_epoch') or 'none'}",
    ]
    if summary.get("gate_id"):
        lines.append(f"gate_id: {summary['gate_id']}")
    if summary.get("evidence_sha256"):
        lines.append(f"evidence_sha256: {summary['evidence_sha256']}")
    if summary.get("path"):
        lines.append(f"path: {summary['path']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _post_backlog_gate_context(profile, run_dir):
    run_dir = Path(run_dir).resolve()
    work_root = Path(profile.get("work_root") or run_dir.parent.parent).resolve()
    frozen_dir = (work_root / "frozen" / run_dir.name).resolve()
    if not (frozen_dir / "taskpack.yaml").is_file():
        return None
    taskpack = json.loads((frozen_dir / "taskpack.yaml").read_text(encoding="utf-8"))
    declarations = taskpack.get("post_backlog_gates")
    if not isinstance(declarations, list) or not declarations:
        return None
    project_root = Path(taskpack["project_root"]).resolve()
    gate_root = run_dir / "state" / "post_backlog_gates"
    return {
        "profile": profile,
        "work_root": work_root,
        "project_root": project_root,
        "run_dir": run_dir,
        "frozen_dir": frozen_dir,
        "taskpack": taskpack,
        "declarations": [dict(gate) for gate in declarations],
        "declarations_by_id": {gate["gate_id"]: dict(gate) for gate in declarations},
        "gate_root": gate_root,
        "epochs_root": gate_root / "epochs",
        "locks_root": gate_root / "locks",
    }


def _initialize_post_backlog_gate_state(profile, run_dir):
    context = _post_backlog_gate_context(profile, run_dir)
    if context is None:
        return None
    state_path = context["gate_root"] / "gate_state.v1.json"
    if state_path.exists():
        return state_path
    value = {
        "schema_version": "post_backlog_gate_state.v1",
        "implementation_run_id": context["run_dir"].name,
        "state": "awaiting_validated_baseline",
        "gate_declaration_sha256": _sha256_json(context["declarations"]),
        "created_at": _format_utc_timestamp(datetime.now(UTC)),
    }
    _atomic_write_json(state_path, value, replace=False)
    return state_path


def _gate_seal_baseline(project_root, profile, run_dir, *, expected_integration_head):
    context = _require_post_backlog_gate_context(profile, run_dir)
    gate_ids = sorted(context["declarations_by_id"])
    with _gate_mutation_locks(context, gate_ids):
        if context["epochs_root"].exists() and any(context["epochs_root"].iterdir()):
            # This also validates a staged/conflicting numeric epoch before rejecting reseal.
            current = _read_current_gate_epoch(context)
            raise AgentTeamCliError(
                "post-backlog gate epoch already exists",
                current_epoch=current["record"]["epoch_number"] if current else None,
            )
        run_status = _build_run_status_summary(profile, context["run_dir"])
        if run_status.get("status") not in {
            "idle",
            "completed",
            "awaiting_post_backlog_gates",
        }:
            raise AgentTeamCliError(
                "frozen backlog must be verified idle before sealing",
                run_status=run_status.get("status") or "unknown",
            )
        task_counts = run_status.get("tasks") or {}
        integration_counts = run_status.get("integration") or {}
        if (
            not int(task_counts.get("total") or 0)
            or int(task_counts.get("done") or 0) != int(task_counts.get("total") or 0)
            or int(task_counts.get("blocked") or 0)
            or int(task_counts.get("ready") or 0)
            or int(integration_counts.get("blocked") or 0)
        ):
            raise AgentTeamCliError(
                "frozen backlog must be fully done with no current integration block",
                tasks=task_counts,
                integration=integration_counts,
            )
        baseline = _paths_integration_baseline(
            context["run_dir"],
            _paths_run_state(context["run_dir"]),
        )
        branch = baseline.get("branch")
        worktree_path = baseline.get("worktree_path")
        if not branch or not worktree_path:
            raise AgentTeamCliError("integration baseline is unavailable for gate seal")
        worktree = Path(worktree_path).resolve()
        actual_head = _git_stdout(project_root, ["rev-parse", "--verify", f"{branch}^{{commit}}"])
        _require_expected_git_oid(
            project_root,
            expected_integration_head,
            actual_head,
            field_name="expected integration head",
        )
        dirty = _git_stdout(worktree, ["status", "--porcelain=v1", "--untracked-files=all"])
        if dirty:
            raise AgentTeamCliError(
                "integration worktree must be clean before gate seal",
                dirty_status=dirty,
            )
        open_invocations = _open_gate_controller_invocations(context)
        if open_invocations:
            raise AgentTeamCliError(
                "open gate controller invocation blocks baseline seal",
                open_controller_invocations=open_invocations,
            )
        command = load_taskpack(context["frozen_dir"])["verification"].get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise AgentTeamCliError("frozen verification command is invalid")
        verification = subprocess.run(
            command,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=3600,
        )
        result_authority = {
            "command": command,
            "returncode": verification.returncode,
            "stdout_sha256": _sha256_bytes(verification.stdout.encode("utf-8")),
            "stderr_sha256": _sha256_bytes(verification.stderr.encode("utf-8")),
        }
        if verification.returncode != 0:
            raise AgentTeamCliError(
                "frozen full verification failed; gate epoch was not published",
                verification_result=result_authority,
            )
        verified_head = _git_stdout(
            project_root,
            ["rev-parse", "--verify", f"{branch}^{{commit}}"],
        )
        if verified_head != actual_head:
            raise AgentTeamCliError(
                "integration baseline changed during frozen verification",
                before_head=actual_head,
                after_head=verified_head,
            )
        dirty_after_verification = _git_stdout(
            worktree,
            ["status", "--porcelain=v1", "--untracked-files=all"],
        )
        if dirty_after_verification:
            raise AgentTeamCliError(
                "frozen verification changed the integration worktree",
                dirty_status=dirty_after_verification,
            )
        target_branch = _git_stdout(project_root, ["symbolic-ref", "--short", "HEAD"])
        target_head = _git_stdout(project_root, ["rev-parse", "HEAD"])
        object_format = _git_object_format(project_root)
        record = {
            "schema_version": "post_backlog_gate_epoch.v1",
            "implementation_run_id": context["run_dir"].name,
            "epoch_number": 1,
            "prior_epoch_sha256": None,
            "gate_declaration_sha256": _sha256_json(context["declarations"]),
            "git_object_format": object_format,
            "target_branch": target_branch,
            "target_head_sha": target_head,
            "integration_branch": branch,
            "integration_head_sha": actual_head,
            "validated_code_sha": actual_head,
            "verification_command_sha256": _sha256_json(command),
            "verification_result_sha256": _sha256_json(result_authority),
            "created_at": _format_utc_timestamp(datetime.now(UTC)),
        }
        _validate_gate_record_schema("post_backlog_gate_epoch.schema.json", record)
        epoch_dir = _publish_gate_epoch(context, record)
        state_path = context["gate_root"] / "gate_state.v1.json"
        _atomic_write_json(
            state_path,
            {
                "schema_version": "post_backlog_gate_state.v1",
                "implementation_run_id": context["run_dir"].name,
                "state": "gates_pending",
                "gate_declaration_sha256": record["gate_declaration_sha256"],
                "current_epoch": 1,
                "current_epoch_sha256": _sha256_json(record),
                "updated_at": _format_utc_timestamp(datetime.now(UTC)),
            },
        )
        return {
            "gate_action": "seal-baseline",
            "gate_status": "pending",
            "taskpack_id": context["run_dir"].name,
            "gate_epoch": 1,
            "epoch_sha256": _sha256_json(record),
            "verification_result": result_authority,
            "path": str(epoch_dir / "epoch.v1.json"),
        }


def _gate_refresh_baseline(
    project_root,
    profile,
    run_dir,
    *,
    expected_gate_epoch,
    expected_target_head,
):
    context = _require_post_backlog_gate_context(profile, run_dir)
    if Path(project_root).resolve() != context["project_root"]:
        raise AgentTeamCliError(
            "selected project root does not match frozen gate authority",
            project_root=str(Path(project_root).resolve()),
            frozen_project_root=str(context["project_root"]),
        )
    gate_ids = sorted(context["declarations_by_id"])
    with _gate_mutation_locks(context, gate_ids):
        current = _require_current_gate_epoch(context, expected_gate_epoch)
        record = current["record"]
        run_status = _build_run_status_summary(profile, context["run_dir"])
        if run_status.get("status") not in {
            "idle",
            "completed",
            "awaiting_post_backlog_gates",
        }:
            raise AgentTeamCliError(
                "implementation run must be idle before gate baseline refresh",
                run_status=run_status.get("status") or "unknown",
            )
        if _open_gate_controller_invocations(context):
            raise AgentTeamCliError(
                "open gate controller invocation blocks baseline refresh",
                open_controller_invocations=_open_gate_controller_invocations(context),
            )

        current_head = _resolved_epoch_integration_head(project_root, record)
        current_worktree = _git_worktree_for_branch(
            project_root,
            record["integration_branch"],
            current_head,
        )
        _require_clean_worktree(
            current_worktree,
            "current gate integration worktree must be clean before baseline refresh",
        )
        relation = _git_completed(
            project_root,
            [
                "merge-base",
                "--is-ancestor",
                record["validated_code_sha"],
                current_head,
            ],
            check=False,
        )
        if relation.returncode != 0:
            raise AgentTeamCliError(
                "current integration branch no longer descends from its validated code",
                validated_code_sha=record["validated_code_sha"],
                current_integration_head=current_head,
            )

        target_head = _git_stdout(
            project_root,
            ["rev-parse", "--verify", f"{record['target_branch']}^{{commit}}"],
        )
        _require_expected_git_oid(
            project_root,
            expected_target_head,
            target_head,
            field_name="expected target head",
        )
        target_worktree = _git_worktree_for_branch(
            project_root,
            record["target_branch"],
            target_head,
        )
        _require_clean_worktree(
            target_worktree,
            "target worktree must be clean before gate baseline refresh",
        )
        command = _frozen_gate_verification_command(context)

        next_epoch = record["epoch_number"] + 1
        branch_base = record["integration_branch"]
        prior_suffix = f"-epoch-{record['epoch_number']}"
        if branch_base.endswith(prior_suffix):
            branch_base = branch_base[: -len(prior_suffix)]
        candidate_branch = f"{branch_base}-epoch-{next_epoch}"
        candidate_worktree = (
            context["run_dir"] / f"integration-epoch-{next_epoch}"
        ).resolve()
        _require_gate_refresh_candidate_absent(
            project_root,
            candidate_branch,
            candidate_worktree,
        )

        published = False
        try:
            created = _git_completed(
                project_root,
                [
                    "worktree",
                    "add",
                    "-b",
                    candidate_branch,
                    str(candidate_worktree),
                    record["validated_code_sha"],
                ],
                check=False,
            )
            if created.returncode != 0:
                raise AgentTeamCliError(
                    "unable to create gate refresh candidate",
                    candidate_branch=candidate_branch,
                    candidate_worktree=str(candidate_worktree),
                    stdout=created.stdout,
                    stderr=created.stderr,
                )
            merged = _git_completed(
                candidate_worktree,
                ["merge", "--no-ff", "--no-edit", target_head],
                check=False,
            )
            if merged.returncode != 0:
                raise AgentTeamCliError(
                    "target merge conflicted; gate epoch was not published",
                    target_head_sha=target_head,
                    candidate_branch=candidate_branch,
                    stdout=merged.stdout,
                    stderr=merged.stderr,
                )
            merge_head = _git_stdout(candidate_worktree, ["rev-parse", "HEAD"])
            target_relation = _git_completed(
                candidate_worktree,
                ["merge-base", "--is-ancestor", target_head, merge_head],
                check=False,
            )
            if target_relation.returncode != 0:
                raise AgentTeamCliError(
                    "refreshed integration head does not descend from target",
                    target_head_sha=target_head,
                    integration_head_sha=merge_head,
                )
            verification = subprocess.run(
                command,
                cwd=candidate_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=3600,
            )
            result_authority = {
                "command": command,
                "returncode": verification.returncode,
                "stdout_sha256": _sha256_bytes(verification.stdout.encode("utf-8")),
                "stderr_sha256": _sha256_bytes(verification.stderr.encode("utf-8")),
            }
            if verification.returncode != 0:
                raise AgentTeamCliError(
                    "frozen full verification failed; gate epoch was not published",
                    verification_result=result_authority,
                )
            verified_head = _git_stdout(candidate_worktree, ["rev-parse", "HEAD"])
            if verified_head != merge_head:
                raise AgentTeamCliError(
                    "gate refresh candidate changed during frozen verification",
                    before_head=merge_head,
                    after_head=verified_head,
                )
            _require_clean_worktree(
                candidate_worktree,
                "frozen verification changed the gate refresh candidate",
            )
            reread = _require_current_gate_epoch(context, expected_gate_epoch)
            if reread["digest"] != current["digest"]:
                raise AgentTeamCliError(
                    "gate epoch changed during baseline refresh",
                    expected_epoch_sha256=current["digest"],
                    current_epoch_sha256=reread["digest"],
                )
            reread_target = _git_stdout(
                project_root,
                ["rev-parse", "--verify", f"{record['target_branch']}^{{commit}}"],
            )
            if reread_target != target_head:
                raise AgentTeamCliError(
                    "target branch changed during baseline refresh",
                    before_head=target_head,
                    after_head=reread_target,
                )
            reread_current_head = _resolved_epoch_integration_head(
                project_root,
                record,
            )
            if reread_current_head != current_head:
                raise AgentTeamCliError(
                    "current integration branch changed during baseline refresh",
                    before_head=current_head,
                    after_head=reread_current_head,
                )
            current_worktree = _git_worktree_for_branch(
                project_root,
                record["integration_branch"],
                current_head,
            )
            _require_clean_worktree(
                current_worktree,
                "current gate integration worktree changed during baseline refresh",
            )
            target_worktree = _git_worktree_for_branch(
                project_root,
                record["target_branch"],
                target_head,
            )
            _require_clean_worktree(
                target_worktree,
                "target worktree changed during baseline refresh",
            )
            open_invocations = _open_gate_controller_invocations(context)
            if open_invocations:
                raise AgentTeamCliError(
                    "controller invocation opened during baseline refresh",
                    open_controller_invocations=open_invocations,
                )
            reread_candidate = _git_stdout(
                project_root,
                ["rev-parse", "--verify", f"{candidate_branch}^{{commit}}"],
            )
            if reread_candidate != merge_head:
                raise AgentTeamCliError(
                    "gate refresh candidate branch changed during verification",
                    before_head=merge_head,
                    after_head=reread_candidate,
                )
            refreshed_record = {
                "schema_version": "post_backlog_gate_epoch.v1",
                "implementation_run_id": context["run_dir"].name,
                "epoch_number": next_epoch,
                "prior_epoch_sha256": current["digest"],
                "gate_declaration_sha256": record["gate_declaration_sha256"],
                "git_object_format": record["git_object_format"],
                "target_branch": record["target_branch"],
                "target_head_sha": target_head,
                "integration_branch": candidate_branch,
                "integration_head_sha": merge_head,
                "validated_code_sha": merge_head,
                "verification_command_sha256": _sha256_json(command),
                "verification_result_sha256": _sha256_json(result_authority),
                "created_at": _format_utc_timestamp(datetime.now(UTC)),
            }
            _validate_gate_record_schema(
                "post_backlog_gate_epoch.schema.json",
                refreshed_record,
            )
            epoch_dir = _publish_gate_epoch(context, refreshed_record)
            published = True
            state_warning = None
            try:
                _atomic_write_json(
                    context["gate_root"] / "gate_state.v1.json",
                    {
                        "schema_version": "post_backlog_gate_state.v1",
                        "implementation_run_id": context["run_dir"].name,
                        "state": "gates_pending",
                        "gate_declaration_sha256": refreshed_record[
                            "gate_declaration_sha256"
                        ],
                        "current_epoch": next_epoch,
                        "current_epoch_sha256": _sha256_json(refreshed_record),
                        "updated_at": _format_utc_timestamp(datetime.now(UTC)),
                    },
                )
            except Exception as exc:
                state_warning = str(exc) or exc.__class__.__name__
            summary = {
                "gate_action": "refresh-baseline",
                "gate_status": "pending",
                "taskpack_id": context["run_dir"].name,
                "gate_epoch": next_epoch,
                "parent_gate_epoch": record["epoch_number"],
                "epoch_sha256": _sha256_json(refreshed_record),
                "target_head_sha": target_head,
                "validated_code_sha": merge_head,
                "integration_branch": candidate_branch,
                "integration_worktree": str(candidate_worktree),
                "verification_result": result_authority,
                "path": str(epoch_dir / "epoch.v1.json"),
            }
            if state_warning:
                summary["state_projection_warning"] = state_warning
            return summary
        finally:
            if not published:
                _remove_unpublished_gate_candidate(
                    project_root,
                    candidate_branch,
                    candidate_worktree,
                )


def _frozen_gate_verification_command(context):
    command = load_taskpack(context["frozen_dir"])["verification"].get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise AgentTeamCliError(
            "frozen taskpack does not expose a deterministic full-verification command"
        )
    return list(command)


def _require_clean_worktree(worktree, message):
    dirty = _git_stdout(
        worktree,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    )
    if dirty:
        raise AgentTeamCliError(message, dirty_status=dirty)


def _require_gate_refresh_candidate_absent(project_root, branch, worktree):
    worktree = Path(worktree).resolve()
    registered = _git_completed(
        project_root,
        ["worktree", "list", "--porcelain"],
        check=False,
    )
    registered_paths = {
        Path(line.partition(" ")[2]).resolve()
        for line in registered.stdout.splitlines()
        if line.startswith("worktree ")
    }
    branch_exists = _git_completed(
        project_root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    ).returncode == 0
    if worktree in registered_paths or worktree.exists() or branch_exists:
        raise AgentTeamCliError(
            "gate refresh candidate already exists; refusing destructive cleanup",
            candidate_branch=branch,
            candidate_branch_exists=branch_exists,
            candidate_worktree=str(worktree),
            candidate_worktree_exists=worktree.exists(),
            candidate_worktree_registered=worktree in registered_paths,
        )


def _remove_unpublished_gate_candidate(project_root, branch, worktree):
    worktree = Path(worktree).resolve()
    registered = _git_completed(
        project_root,
        ["worktree", "list", "--porcelain"],
        check=False,
    )
    registered_paths = {
        Path(line.partition(" ")[2]).resolve()
        for line in registered.stdout.splitlines()
        if line.startswith("worktree ")
    }
    if worktree in registered_paths:
        _git_completed(
            project_root,
            ["worktree", "remove", "--force", str(worktree)],
            check=False,
        )
    elif worktree.exists():
        raise AgentTeamCliError(
            "unregistered gate refresh candidate path already exists",
            candidate_worktree=str(worktree),
        )
    branch_exists = _git_completed(
        project_root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    if branch_exists.returncode == 0:
        deleted = _git_completed(
            project_root,
            ["branch", "-D", branch],
            check=False,
        )
        if deleted.returncode != 0:
            raise AgentTeamCliError(
                "unable to remove unpublished gate refresh branch",
                candidate_branch=branch,
                stderr=deleted.stderr,
            )


def _gate_register(
    project_root,
    profile,
    run_dir,
    *,
    gate_id,
    gate_epoch,
    evidence_run_id,
    expected_integration_head,
):
    context = _require_post_backlog_gate_context(profile, run_dir)
    declaration = _require_gate_declaration(context, gate_id)
    with _gate_mutation_locks(context, sorted(context["declarations_by_id"])):
        current = _require_current_gate_epoch(context, gate_epoch)
        head = _resolved_epoch_integration_head(project_root, current["record"])
        _require_expected_git_oid(
            project_root,
            expected_integration_head,
            head,
            field_name="expected integration head",
        )
        if head != current["record"]["integration_head_sha"]:
            raise AgentTeamCliError(
                "gate evidence must be registered before the integration baseline changes",
                epoch_integration_head=current["record"]["integration_head_sha"],
                current_integration_head=head,
            )
        if _open_gate_controller_invocations(context, gate_id=gate_id):
            raise AgentTeamCliError(
                "open gate controller invocation blocks evidence registration",
                gate_id=gate_id,
            )
        evidence_run = (context["work_root"] / "runs" / evidence_run_id).resolve()
        runs_root = (context["work_root"] / "runs").resolve()
        _require_path_within(evidence_run, runs_root, "evidence run")
        if not evidence_run.is_dir() or evidence_run.parent != runs_root:
            raise AgentTeamCliError(
                "evidence run must exist directly under the configured work root",
                evidence_run=str(evidence_run),
            )
        if (context["run_dir"] / "state" / "run_identity.v1.json").is_file():
            validate_acceptance_run_identity(
                evidence_run,
                expected_project_key=(
                    _read_json_if_exists(
                        context["run_dir"] / "state" / "run_identity.v1.json"
                    ).get("project_key")
                ),
                expected_implementation_run_id=context["run_dir"].name,
                expected_gate_epoch=gate_epoch,
            )
        artifact_relative = declaration["evidence_artifact"]
        artifact_path = (evidence_run / artifact_relative).resolve()
        _require_path_within(artifact_path, evidence_run, "evidence artifact")
        receipt_path = _gate_receipt_path(context, current["record"], gate_id)
        previous = _read_json_if_exists(receipt_path)
        new_binding = {
            "evidence_run_id": evidence_run_id,
            "evidence_run_relative_path": evidence_run.relative_to(context["work_root"]).as_posix(),
            "expected_integration_head_sha": head,
        }
        if previous:
            previous_binding = {
                key: previous.get(key) for key in new_binding
            }
            previous_decision = _evaluate_one_post_backlog_gate(
                context,
                current,
                declaration,
                prior_decisions={},
            )
            if previous_decision.get("state") == "passed":
                if previous_binding == new_binding:
                    summary = {
                        "gate_action": "register",
                        "gate_status": "passed",
                        "taskpack_id": context["run_dir"].name,
                        "gate_epoch": gate_epoch,
                        "gate_id": gate_id,
                        "path": str(receipt_path),
                        "idempotent": True,
                    }
                    completion = _complete_gated_milestone_if_ready(context)
                    if completion is not None:
                        summary["run_completion"] = completion
                    return summary
                raise AgentTeamCliError("a passed gate receipt cannot be replaced", gate_id=gate_id)
        attempts = list(previous.get("attempt_history", [])) if previous else []
        if previous:
            attempts.append(
                {
                    "evidence_run_id": previous.get("evidence_run_id"),
                    "evidence_run_relative_path": previous.get("evidence_run_relative_path"),
                    "registered_at": previous.get("registered_at"),
                    "superseded_at": _format_utc_timestamp(datetime.now(UTC)),
                }
            )
        receipt = {
            "schema_version": "post_backlog_gate_receipt.v1",
            "implementation_run_id": context["run_dir"].name,
            "epoch_number": gate_epoch,
            "epoch_sha256": current["digest"],
            "gate_id": gate_id,
            **new_binding,
            "git_object_format": current["record"]["git_object_format"],
            "evidence_artifact": artifact_relative,
            "evidence_schema": declaration["evidence_schema"],
            "attempt_history": attempts[-20:],
            "registered_at": _format_utc_timestamp(datetime.now(UTC)),
        }
        _validate_gate_record_schema("post_backlog_gate_receipt.schema.json", receipt)
        _atomic_write_json(receipt_path, receipt)
        summary = {
            "gate_action": "register",
            "gate_status": "pending",
            "taskpack_id": context["run_dir"].name,
            "gate_epoch": gate_epoch,
            "gate_id": gate_id,
            "path": str(receipt_path),
            "artifact_path": str(artifact_path),
            "attempt_count": len(attempts) + 1,
        }
        completion = _complete_gated_milestone_if_ready(context)
        if completion is not None:
            summary["gate_status"] = "passed"
            summary["run_completion"] = completion
        return summary


def _gate_approve(
    project_root,
    profile,
    run_dir,
    *,
    gate_id,
    gate_epoch,
    expected_evidence_sha256,
    expected_integration_head,
):
    context = _require_post_backlog_gate_context(profile, run_dir)
    declaration = _require_gate_declaration(context, gate_id)
    if not declaration.get("operator_review_required"):
        raise AgentTeamCliError("gate does not accept operator approval", gate_id=gate_id)
    _require_operator_approval_context()
    confirmation = f"approve {context['run_dir'].name} {gate_id} epoch {gate_epoch}"
    with _gate_mutation_locks(context, sorted(context["declarations_by_id"])):
        sys.stderr.write(
            "Review the final report, diff, coverage, evidence digests, and integration head.\n"
            f"Type exactly `{confirmation}` to publish approval: "
        )
        sys.stderr.flush()
        if sys.stdin.readline().strip() != confirmation:
            raise AgentTeamCliError("literal operator approval confirmation did not match")
        current = _require_current_gate_epoch(context, gate_epoch)
        decisions = _evaluate_post_backlog_gates(context, current=current)
        decision = next(item for item in decisions["gates"] if item["gate_id"] == gate_id)
        if decision.get("state") not in {"awaiting_operator_review", "passed"}:
            raise AgentTeamCliError(
                "gate evidence is not ready for operator approval",
                gate_id=gate_id,
                derived_state=decision.get("state"),
                reasons=decision.get("reasons"),
            )
        head = _resolved_epoch_integration_head(project_root, current["record"])
        _require_expected_git_oid(
            project_root,
            expected_integration_head,
            head,
            field_name="expected integration head",
        )
        if decision.get("evidence_sha256") != expected_evidence_sha256:
            raise AgentTeamCliError(
                "expected evidence digest does not match current evidence",
                expected_evidence_sha256=expected_evidence_sha256,
                actual_evidence_sha256=decision.get("evidence_sha256"),
            )
        approval_path = _gate_approval_path(context, current["record"], gate_id)
        if decision.get("state") == "passed":
            summary = {
                "gate_action": "approve",
                "gate_status": "passed",
                "taskpack_id": context["run_dir"].name,
                "gate_epoch": gate_epoch,
                "gate_id": gate_id,
                "evidence_sha256": expected_evidence_sha256,
                "path": str(approval_path),
                "idempotent": True,
            }
            completion = _complete_gated_milestone_if_ready(context)
            if completion is not None:
                summary["run_completion"] = completion
            return summary
        _require_clean_gate_schema_paths(
            _gate_integration_worktree(context, current["record"]),
            declaration,
        )
        diff_sha256 = _gate_review_diff_sha256(
            project_root,
            current["record"],
            head,
            expected_paths=(
                _PHASE1_REPORT_REVIEW_PATHS if gate_id == "P1-06E" else None
            ),
        )
        approval = {
            "schema_version": "post_backlog_gate_approval.v1",
            "implementation_run_id": context["run_dir"].name,
            "epoch_number": gate_epoch,
            "epoch_sha256": current["digest"],
            "gate_id": gate_id,
            "decision": "approved",
            "evidence_sha256": expected_evidence_sha256,
            "review_diff_sha256": diff_sha256,
            "final_report_sha": head,
            "git_object_format": current["record"]["git_object_format"],
            "operator_identity": f"{getpass.getuser()} (uid={os.getuid()})",
            "reviewed_at": _format_utc_timestamp(datetime.now(UTC)),
        }
        _validate_schema_from_git(
            project_root,
            head,
            declaration["operator_approval_schema"],
            approval,
        )
        existing = _read_json_if_exists(approval_path)
        if existing:
            if _canonical_json_bytes(existing) == _canonical_json_bytes(approval):
                idempotent = True
            else:
                raise AgentTeamCliError("conflicting operator approval cannot replace immutable record")
        else:
            _atomic_write_json(approval_path, approval, replace=False)
            idempotent = False
        final = _evaluate_post_backlog_gates(context, current=current)
        final_decision = next(item for item in final["gates"] if item["gate_id"] == gate_id)
        if final_decision.get("state") != "passed":
            raise AgentTeamCliError(
                "published approval did not satisfy current gate",
                reasons=final_decision.get("reasons"),
            )
        summary = {
            "gate_action": "approve",
            "gate_status": "passed",
            "taskpack_id": context["run_dir"].name,
            "gate_epoch": gate_epoch,
            "gate_id": gate_id,
            "evidence_sha256": expected_evidence_sha256,
            "path": str(approval_path),
            "idempotent": idempotent,
        }
        completion = _complete_gated_milestone_if_ready(context)
        if completion is not None:
            summary["run_completion"] = completion
        return summary


def _handle_notify(args):
    if args.notify_command == "test":
        summary = _notify_test(args)
        if args.json:
            return summary
        _write_notify_text(summary)
        return 0
    if args.notify_command == "run-completed":
        summary = _notify_run_completed(args)
        if args.json:
            return summary
        _write_notify_text(summary)
        return 0
    if args.notify_command == "diagnose":
        summary = _notify_diagnose(args)
        if args.json:
            return summary
        _write_notify_diagnose_text(summary)
        return 0
    raise AgentTeamCliError("unknown notify command", command=args.notify_command)


def _notify_test(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    project = (
        args.notification_project
        or profile.get("notification_project")
        or profile.get("project_key")
        or project_root.name
    )
    webhook_env = _profile_feishu_value(args, profile, "webhook_env")
    signing_secret_env = _profile_feishu_value(args, profile, "signing_secret_env")
    if not webhook_env:
        raise AgentTeamCliError(
            "Feishu webhook env is not configured",
            project=str(project_root),
        )
    webhook_url = os.environ.get(webhook_env)
    if not webhook_url:
        raise AgentTeamCliError(
            "Feishu webhook env value is not set",
            webhook_env=webhook_env,
            project=str(project_root),
        )
    signing_secret = os.environ.get(signing_secret_env) if signing_secret_env else None
    event = _notify_test_event(project, message=args.message)
    summary = {
        "notify_status": "dry_run" if args.dry_run else "pending",
        "provider": "feishu",
        "project": project,
        "project_root": str(project_root),
        "webhook_env": webhook_env,
        "webhook_env_set": True,
        "signing_secret_env": signing_secret_env,
        "signing_enabled": bool(signing_secret),
        "event_type": event["event_type"],
        "run_dir": str(project_root),
    }
    if args.dry_run:
        return summary

    notifier = FeishuWebhookNotifier(
        webhook_url=webhook_url,
        signing_secret=signing_secret,
        project=project,
    )
    result = notifier.notify_event(event, run_dir=str(project_root))
    payload = result.get("payload") if isinstance(result, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    summary.update(
        {
            "notify_status": payload.get("notification_status") or "unknown",
            "source_event_type": payload.get("source_event_type"),
            "message_summary": payload.get("message_summary"),
        }
    )
    if payload.get("error_class"):
        summary["error_class"] = payload["error_class"]
    if payload.get("error_summary"):
        summary["error_summary"] = payload["error_summary"]
    if summary["notify_status"] != "sent":
        raise AgentTeamCliError("Feishu notification test failed", **summary)
    return summary


def _notify_run_completed(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    run_dir = _selected_run_dir(args, profile, command_name="notify run-completed")
    project = (
        args.notification_project
        or profile.get("notification_project")
        or profile.get("project_key")
        or project_root.name
    )
    webhook_env = _profile_feishu_value(args, profile, "webhook_env")
    signing_secret_env = _profile_feishu_value(args, profile, "signing_secret_env")
    if not webhook_env:
        raise AgentTeamCliError(
            "Feishu webhook env is not configured",
            project=str(project_root),
        )
    webhook_url = os.environ.get(webhook_env)
    if not webhook_url:
        raise AgentTeamCliError(
            "Feishu webhook env value is not set",
            webhook_env=webhook_env,
            project=str(project_root),
        )
    signing_secret = os.environ.get(signing_secret_env) if signing_secret_env else None
    gate_summary = _post_backlog_gate_summary(profile, run_dir)
    if gate_summary is not None and not gate_summary.get("all_passed"):
        raise AgentTeamCliError(
            "run-completed notification is blocked by required post-backlog gates",
            post_backlog_gates=gate_summary.get("gates"),
            next_action=gate_summary.get("next_action"),
        )
    report = build_run_completion_report(
        run_dir,
        project=project,
        write_files=False,
    )
    event = _run_completed_notification_event(report)
    summary = {
        "notify_status": "dry_run" if args.dry_run else "pending",
        "provider": "feishu",
        "project": project,
        "project_root": str(project_root),
        "taskpack_id": run_dir.name,
        "run_dir": str(run_dir),
        "webhook_env": webhook_env,
        "webhook_env_set": True,
        "signing_secret_env": signing_secret_env,
        "signing_enabled": bool(signing_secret),
        "event_type": event["event_type"],
        "run_status": report.get("run_status"),
        "task_count": report.get("task_count", 0),
        "blocked_count": report.get("blocked_count", 0),
    }
    if args.dry_run:
        return summary

    notifier = FeishuWebhookNotifier(
        webhook_url=webhook_url,
        signing_secret=signing_secret,
        project=project,
    )
    result = notifier.notify_event(event, run_dir=str(run_dir))
    payload = result.get("payload") if isinstance(result, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    summary.update(
        {
            "notify_status": payload.get("notification_status") or "unknown",
            "source_event_type": payload.get("source_event_type"),
            "message_summary": payload.get("message_summary"),
        }
    )
    if payload.get("error_class"):
        summary["error_class"] = payload["error_class"]
    if payload.get("error_summary"):
        summary["error_summary"] = payload["error_summary"]
    if summary["notify_status"] != "sent":
        raise AgentTeamCliError("Feishu run-completed notification failed", **summary)
    return summary


def _notify_diagnose(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    project = (
        args.notification_project
        or profile.get("notification_project")
        or profile.get("project_key")
        or project_root.name
    )
    webhook_env = _profile_feishu_value(args, profile, "webhook_env")
    signing_secret_env = _profile_feishu_value(args, profile, "signing_secret_env")
    if not webhook_env:
        raise AgentTeamCliError(
            "Feishu webhook env is not configured",
            project=str(project_root),
        )
    webhook_url = os.environ.get(webhook_env)
    if not webhook_url:
        raise AgentTeamCliError(
            "Feishu webhook env value is not set",
            webhook_env=webhook_env,
            project=str(project_root),
        )
    signing_secret = os.environ.get(signing_secret_env) if signing_secret_env else None
    diagnosis = diagnose_feishu_webhook_delivery(
        webhook_url=webhook_url,
        signing_secret=signing_secret,
        project=project,
        dry_run=args.dry_run,
        message=args.message,
    )
    delivery_variants = diagnosis.get("delivery_variants") or []
    variants = []
    for variant in delivery_variants:
        if not isinstance(variant, dict):
            continue
        variant_summary = {
            "variant": variant.get("variant"),
            "status": variant.get("notification_status") or "unknown",
        }
        for field in (
            "msg_type",
            "content_length",
            "signed",
            "delivery_attempt_count",
            "max_delivery_attempts",
            "error_class",
            "error_summary",
        ):
            if field in variant:
                variant_summary[field] = variant[field]
        variants.append(variant_summary)
    return {
        "diagnosis_status": diagnosis.get("diagnosis_status") or "unknown",
        "provider": diagnosis.get("provider") or "feishu",
        "project": project,
        "project_root": str(project_root),
        "webhook_env": webhook_env,
        "webhook_env_set": bool(webhook_url),
        "signing_secret_env": signing_secret_env,
        "signing_enabled": bool(signing_secret),
        "dry_run": bool(args.dry_run),
        "variants": variants,
        "delivery_variants": delivery_variants,
    }


def _run_completed_notification_event(report):
    run_id = report.get("run_id") or "unknown"
    return {
        "event_id": f"notify-run-completed-{run_id}",
        "sequence": 0,
        "event_type": "run_completed",
        "actor": "agentteam-cli",
        "target_agent_id": None,
        "idempotency_key": f"notify:run_completed:{run_id}",
        "correlation_id": f"run:{run_id}",
        "payload": {
            "run_status": report.get("run_status") or "completed",
            "scheduler_status": report.get("scheduler_status"),
            "task_count": report.get("task_count", 0),
            "blocked_count": report.get("blocked_count", 0),
            "operator_report": report.get("operator_report") or {},
            "worker_diagnostics": report.get("worker_diagnostics") or {},
        },
    }


def _notify_test_event(project, message=None):
    task_message = message or f"AgentTeam notification test for {project}."
    return {
        "event_id": "notify-test",
        "sequence": 0,
        "event_type": "run_completed",
        "actor": "agentteam-cli",
        "target_agent_id": None,
        "idempotency_key": "notify:test",
        "correlation_id": "notify:test",
        "payload": {
            "run_status": "diagnostic",
            "operator_report": {
                "report_schema_version": "operator_run_report.v1",
                "token_usage": {
                    "usage_status": "not_applicable",
                    "reason": "diagnostic notification; no AgentTeam run",
                },
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [
                    {
                        "task_id": "notify-test",
                        "status": "implementation completed",
                        "what_changed": [task_message],
                        "changed_files": [],
                        "verification": ["Feishu webhook delivery test was triggered."],
                        "integration": "not requested",
                        "merge_recommendation": "No merge action; notification test only.",
                        "next_steps": [
                            "If you receive this message, Feishu notification delivery works."
                        ],
                    }
                ],
            },
        },
    }


def _handle_update(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    if args.status:
        summary = update_status(profile)
    elif args.source_checkout:
        summary = install_release_from_checkout(
            args.source_checkout,
            work_root,
            release_id=args.release_id,
            activate=True,
        )
        summary["project"] = profile.get("project_key") or "unknown"
    elif args.source_git:
        summary = install_release_from_git(
            args.source_git,
            args.source_ref,
            work_root,
            release_id=args.release_id,
            activate=True,
        )
        summary["project"] = profile.get("project_key") or "unknown"
    elif args.activate:
        summary = {
            "update_status": "activated",
            "project": profile.get("project_key") or "unknown",
            "active_release": activate_release(work_root, args.activate),
            "known_releases": update_status(profile)["known_releases"],
        }
    elif args.rollback:
        summary = {
            "update_status": "rollback_activated",
            "project": profile.get("project_key") or "unknown",
            "active_release": activate_release(work_root, args.rollback, update_status="rollback_activated"),
            "known_releases": update_status(profile)["known_releases"],
        }
    elif args.prune:
        summary = {
            "update_status": "pruned",
            "project": profile.get("project_key") or "unknown",
            "active_release": update_status(profile)["active_release"],
            "known_releases": update_status(profile)["known_releases"],
            "release_prune": prune_releases(work_root, keep_latest=1),
        }
        summary["known_releases"] = update_status(profile)["known_releases"]
    elif args.adopt_run:
        if not args.force:
            raise AgentTeamCliError("--force is required for immutable legacy-run adoption")
        frozen_taskpack = work_root / "frozen" / args.adopt_run / "taskpack.yaml"
        if frozen_taskpack.is_file():
            frozen = _read_json_if_exists(frozen_taskpack)
            context = frozen.get("context") if isinstance(frozen, dict) else None
            if isinstance(context, dict) and any(
                context.get(key)
                for key in (
                    "runtime_release_id",
                    "runtime_release_source_commit",
                    "git_object_format",
                )
            ):
                raise AgentTeamCliError(
                    "approval-bound taskpack runs cannot use legacy adoption",
                    taskpack_id=args.adopt_run,
                )
        release = (
            selected_release_identity(work_root, args.release_id)
            if args.release_id
            else active_release_identity(work_root)
        )
        pair = adopt_legacy_implementation_run(
            work_root,
            project_key=profile.get("project_key") or project_root.name,
            run_id=args.adopt_run,
            taskpack_id=args.adopt_run,
            release_identity=release,
        )
        summary = {
            "update_status": "legacy_run_adopted",
            "project": profile.get("project_key") or "unknown",
            "run_id": args.adopt_run,
            "run_dir": pair["run_dir"],
            "creation_sequence": pair["identity"]["creation_sequence"],
            "runtime_release_binding": pair["binding"],
        }
    else:
        raise AgentTeamCliError("update action is required")
    summary = _attach_release_status_fields(summary, profile)
    active_release = summary.get("active_release")
    if isinstance(active_release, dict) and isinstance(active_release.get("release_event"), dict):
        summary["release_event"] = active_release["release_event"]
    if args.json:
        return summary
    _write_update_text(summary)
    return 0


def _handle_answer(args):
    return answer_manual_gate(
        args.run_dir,
        args.question_id,
        args.answer,
        operator=args.operator,
    )


def _handle_permissions(args):
    if args.permission_command == "list":
        return list_permission_requests(args.run_dir)
    if args.permission_command == "approve":
        return resolve_permission_request(
            args.run_dir,
            args.request_id,
            "approved",
            operator=args.operator,
            reason=args.reason,
        )
    if args.permission_command == "deny":
        return resolve_permission_request(
            args.run_dir,
            args.request_id,
            "denied",
            operator=args.operator,
            reason=args.reason,
        )
    raise AgentTeamCliError("unknown permissions command", command=args.permission_command)


def _handle_resume(args):
    resume_context = _load_resume_context(args.run_dir)
    all_waiting_gates = _waiting_manual_gates_from_snapshot(resume_context["snapshot"])
    if args.list:
        return _waiting_manual_gates_summary(args.run_dir, all_waiting_gates, resume_context)
    if not args.interactive:
        raise AgentTeamCliError("--interactive is required for resume", missing_argument="--interactive")
    if not all_waiting_gates:
        return {
            "resume_status": "no_waiting_manual_gate",
            "answered_count": 0,
            "answered": [],
            "run_dir": str(Path(args.run_dir).resolve()),
        }
    waiting_gates = _selected_waiting_manual_gates(args.question_id, all_waiting_gates)

    answered = []
    for gate in waiting_gates:
        answer = _prompt_manual_gate_answer(gate, resume_context)
        answered.append(
            answer_manual_gate(
                args.run_dir,
                gate["question_id"],
                answer,
                operator=args.operator,
            )
        )

    return {
        "resume_status": "answered_manual_gate",
        "answered_count": len(answered),
        "answered": answered,
        "run_dir": str(Path(args.run_dir).resolve()),
    }


def _handle_stop(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    if args.authoring:
        summary = _stop_authoring(profile, grace_seconds=args.grace_seconds, force=args.force, operator=args.operator)
        summary["project"] = profile.get("project_key") or "unknown"
    elif args.stale and not args.taskpack and not args.run_dir:
        summary = cleanup_stale_runs(profile, operator=args.operator)
        summary["project"] = profile.get("project_key") or "unknown"
    else:
        run_dir = _selected_run_dir(args, profile, command_name="stop")
        if not run_dir.exists():
            raise AgentTeamCliError("run not found", run_dir=str(run_dir))
        summary = stop_run(
            run_dir,
            grace_seconds=args.grace_seconds,
            force=args.force,
            stale_only=args.stale,
            operator=args.operator,
        )
        summary["project"] = profile.get("project_key") or "unknown"
    if args.json:
        return summary
    _write_stop_text(summary)
    return 0


def _handle_watch(args):
    profile = _watch_profile(args)
    run_dir = _watch_run_dir(args, profile)
    if not run_dir.exists():
        raise AgentTeamCliError("run not found", run_dir=str(run_dir))
    max_lines = args.max_lines
    if max_lines is None and args.interval <= 0:
        max_lines = 1
    cursor = 0
    printed = 0
    while max_lines is None or printed < max_lines:
        summary = _build_run_status_summary(profile, run_dir)
        _record_run_stale_detected_if_needed(run_dir, summary)
        cursor, events = read_event_records_since(run_dir / "events.jsonl", cursor, max_records=20)
        _write_watch_line(summary, events, json_lines=args.json_lines)
        printed += 1
        if _watch_should_stop(summary):
            break
        if max_lines is not None and printed >= max_lines:
            break
        time.sleep(max(args.interval, 0))
    return 0


def _handle_report(args):
    profile = _watch_profile(args)
    run_dir = _watch_run_dir(args, profile)
    if not run_dir.exists():
        raise AgentTeamCliError("run not found", run_dir=str(run_dir))
    work_root = Path(profile.get("work_root") or run_dir.parent.parent).resolve()
    projected_run = read_projected_run_metadata(
        work_root,
        run_dir.name,
    )
    projection_metadata = (
        _projection_output_metadata(projected_run)
        if projected_run is not None
        else _projection_check_metadata(work_root)
    )
    report = _build_gate_guided_completion_report(
        run_dir,
        project=profile.get("project_key") or "unknown",
        profile=profile,
    )
    gate_summary = _post_backlog_gate_summary(profile, run_dir)
    artifact_snapshot = snapshot_run_artifacts_safe(
        profile.get("work_root") or run_dir.parent,
        run_dir,
        taskpack_id=run_dir.name,
        project=profile.get("project_key") or "unknown",
    )
    if args.json:
        report = {
            **report,
            "artifact_snapshot": artifact_snapshot,
            **projection_metadata,
            "projection_run": projected_run["run"] if projected_run is not None else None,
        }
        return report
    rendered = render_run_completion_report(report)
    sys.stdout.write(rendered)
    if gate_summary is not None:
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.write(
            f"post_backlog_gates: {gate_summary.get('state') or 'unknown'}\n"
            f"gate_next_action: {gate_summary.get('next_action') or 'none'}\n"
        )
        for gate in gate_summary.get("gates") or []:
            sys.stdout.write(
                f"post_backlog_gate: {gate.get('gate_id') or 'unknown'} "
                f"status={gate.get('status') or 'unknown'} "
                f"state={gate.get('state') or 'unknown'}\n"
            )
    if projection_metadata:
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.write("\n".join(_projection_text_lines(projection_metadata)) + "\n")
    sys.stdout.flush()


def _build_gate_guided_completion_report(
    run_dir,
    *,
    project,
    profile,
    write_files=True,
):
    report = build_run_completion_report(
        run_dir,
        project=project,
        write_files=write_files,
    )
    gate_summary = _post_backlog_gate_summary(profile, run_dir)
    if gate_summary is None:
        return report
    report = _apply_post_backlog_gate_report_guidance(report, gate_summary)
    if write_files:
        Path(report["report_path"]).write_text(
            render_run_completion_report(report),
            encoding="utf-8",
        )
        _write_json(report["report_json_path"], report)
    return report


def _apply_post_backlog_gate_report_guidance(report, gate_summary):
    report = dict(report)
    report["post_backlog_gates"] = gate_summary
    operator_view = (
        gate_summary.get("operator_view")
        if isinstance(gate_summary.get("operator_view"), dict)
        else {}
    )
    commands = dict(operator_view.get("review_commands") or {})
    report["operator_review"] = operator_view
    if isinstance(operator_view.get("integration_baseline"), dict):
        report["integration_baseline"] = operator_view["integration_baseline"]
    report["review_hint"] = (
        gate_summary.get("next_action")
        or commands.get("integrate")
        or operator_view.get("repair_action")
    )
    completion = (
        dict(report.get("completion_summary"))
        if isinstance(report.get("completion_summary"), dict)
        else {}
    )
    review_gate = (
        dict(completion.get("review_gate"))
        if isinstance(completion.get("review_gate"), dict)
        else {}
    )
    baseline = operator_view.get("integration_baseline") or {}
    gate_review = operator_view.get("review_gate") or {}
    review_gate.update(
        {
            "status": (
                gate_review.get("state")
                or (
                    "passed"
                    if gate_summary.get("all_passed")
                    else gate_summary.get("state")
                )
            ),
            "gate_epoch": operator_view.get("gate_epoch"),
            "gate_id": gate_review.get("gate_id"),
            "integration_branch": operator_view.get("integration_branch"),
            "base_head": baseline.get("base_sha"),
            "baseline_head": operator_view.get("integration_head_sha"),
            "historical_scheduler_head": operator_view.get(
                "historical_scheduler_head_sha"
            ),
            "integration_worktree": operator_view.get("integration_worktree"),
            "integration_head_relation": gate_review.get(
                "integration_head_relation"
            ),
            "commit_field": gate_review.get("commit_field"),
            "report_paths": gate_review.get("changed_paths"),
            "expected_report_paths": gate_review.get("expected_report_paths"),
            "report_command": commands.get("report"),
            "paths_command": commands.get("paths"),
            "diff_command": commands.get("diff"),
            "approval_command": commands.get("approve"),
            "integrate_command": commands.get("integrate"),
            "validated_approval_identity": (
                (gate_review.get("validated_approval") or {}).get(
                    "operator_identity"
                )
            ),
            "repair_action": operator_view.get("repair_action"),
        }
    )
    completion["review_gate"] = {
        key: value for key, value in review_gate.items() if value is not None
    }
    report["completion_summary"] = completion
    if gate_summary.get("all_passed"):
        completion["integration_recommendation"] = (
            "Review the fresh gate-bound report and diff, then run "
            f"`{commands.get('integrate')}`."
            if commands.get("integrate")
            else "The gate-bound integration view is unavailable; fail closed."
        )
        return report
    report["run_status"] = "awaiting_post_backlog_gates"
    report["scheduler_status"] = "awaiting_post_backlog_gates"
    completion["integration_recommendation"] = (
        "Complete the declared post-backlog gates before integration: "
        f"{gate_summary.get('next_action') or 'review gate status'}."
    )
    completion["status_line"] = (
        "awaiting_post_backlog_gates: backlog verified idle; milestone incomplete"
    )
    completion["review_gate"]["status"] = (
        "failed_closed"
        if gate_summary.get("state") == "failed_closed"
        else "post_backlog_gates_pending"
    )
    completion["review_gate"].pop("integrate_command", None)
    completion["review_gate"]["gate_command"] = gate_summary.get("next_action")
    report["completion_summary"] = completion
    return report
    return 0


def _handle_chat(args):
    profile = _watch_profile(args)
    run_dir = _watch_run_dir(args, profile)
    if not run_dir.exists():
        raise AgentTeamCliError("run not found", run_dir=str(run_dir))
    context = build_runtime_diagnostic_context(run_dir, topic=args.topic)
    if args.interactive:
        result = run_runtime_diagnostic_chat(
            context,
            codex_command=args.codex_command,
            model=args.codex_model,
            timeout_seconds=args.codex_timeout_seconds,
        )
        return result["exit_code"]
    if args.json:
        return context
    sys.stdout.write(render_runtime_diagnostic_context(context))
    sys.stdout.flush()
    return 0


def _handle_grounding(args):
    project_root = Path(args.project_root or ".").resolve()
    project = project_root.name
    profile_warning = None
    try:
        profile = load_project_profile(project_root)
        project = profile.get("project_key") or project
    except AgentTeamProfileError as exc:
        profile_warning = {
            "warning": "profile_unavailable",
            "message": str(exc),
        }
    grounding = build_repo_grounding(project_root)
    grounding["project"] = project
    if profile_warning:
        grounding["scan_status"] = "degraded"
        grounding.setdefault("warnings", []).append(profile_warning)
    if args.json:
        return grounding
    sys.stdout.write(render_repo_grounding_text(grounding))
    sys.stdout.flush()
    return 0


def _handle_doctor(args):
    if args.invocation_supervision_probe:
        summary = _run_invocation_supervision_probe()
        if args.json:
            _print_json(summary, stream=sys.stdout)
        else:
            _write_invocation_supervision_probe_text(summary)
        return 0 if summary["status"] == "passed" else 1
    project_root = Path(args.project_root or ".").resolve()
    summary = _build_doctor_summary(project_root)
    if args.json:
        return summary
    _write_doctor_text(summary)
    return 0


def _handle_logs(args):
    profile = _watch_profile(args)
    run_dir = _selected_run_dir(args, profile, command_name="logs")
    if not run_dir.exists():
        raise AgentTeamCliError("run not found", run_dir=str(run_dir))
    work_root = Path(profile.get("work_root") or run_dir.parent.parent).resolve()
    projected = read_projected_run_events(work_root, run_dir.name, include_fallback_status=True)
    projection_metadata = _projection_output_metadata(projected)
    if projected is not None and projected.get("projection_source") == "db":
        events = projected["events"]
    else:
        if not projection_metadata:
            projection_metadata = _projection_check_metadata(work_root)
        events = _read_jsonl(run_dir / "events.jsonl")
    line_count = max(int(args.lines or 0), 0)
    selected = events[-line_count:] if line_count else []
    summary = {
        "logs_status": "ready",
        "project": profile.get("project_key") or "unknown",
        "latest_run": run_dir.name,
        "run_dir": str(run_dir),
        **projection_metadata,
        "event_count": len(events),
        "returned_count": len(selected),
        "events": selected,
    }
    if args.json:
        return summary
    _write_logs_text(summary)
    return 0


def _handle_explain_status(args):
    profile = _watch_profile(args)
    run_dir = _selected_run_dir(args, profile, command_name="explain-status")
    if not run_dir.exists():
        raise AgentTeamCliError("run not found", run_dir=str(run_dir))
    status_summary = _build_run_status_summary(profile, run_dir)
    explanation = _status_explanation(status_summary)
    summary = {
        "explain_status": "ready",
        "project": status_summary["project"],
        "latest_run": status_summary["latest_run"],
        "overall_status": status_summary.get("overall_status") or status_summary.get("status"),
        "run_status": status_summary.get("run_status") or status_summary.get("status"),
        "liveness_status": status_summary.get("liveness_status"),
        "active_phase": status_summary.get("active_phase"),
        "explanation": explanation,
        "next_action": _status_next_action(status_summary),
        "permission_request_details": status_summary.get("permission_request_details") or [],
        "run_dir": status_summary["run_dir"],
    }
    if args.json:
        return {**summary, "status_summary": status_summary}
    _write_explain_status_text(summary)
    return 0


def _handle_gc(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    if args.delete_artifacts and not args.artifacts:
        raise AgentTeamCliError("--delete-artifacts requires --artifacts", missing_argument="--artifacts")
    if args.delete_artifacts and not args.force:
        raise AgentTeamCliError("--delete-artifacts requires --force", missing_argument="--force")
    current_update_status = update_status(profile)
    if args.force:
        release_prune = prune_releases(work_root, keep_latest=args.keep_releases)
        gc_status = "completed"
    else:
        known_releases = current_update_status.get("known_releases") or []
        release_prune = {
            "prune_status": "dry_run",
            "keep_latest": max(0, int(args.keep_releases or 0)),
            "known_release_ids": [
                release.get("release_id")
                for release in known_releases
                if isinstance(release, dict) and release.get("release_id")
            ],
            "force_required": True,
        }
        gc_status = "dry_run"
    stale_cleanup = None
    if args.stale_runs:
        stale_cleanup = cleanup_stale_runs(profile, operator="agentteam-gc") if args.force else {
            "cleanup_status": "dry_run",
            "force_required": True,
        }
    global_release_prune = None
    if args.global_releases:
        global_release_prune = prune_global_releases(work_root, force=args.force)
    artifact_projection = _gc_artifact_projection_summary(work_root)
    artifact_retention_plan = None
    if args.artifacts:
        artifact_retention_plan = _gc_artifact_retention_plan(
            work_root,
            limit=args.artifact_limit,
            delete_artifacts=args.delete_artifacts,
        )
    summary = {
        "gc_status": gc_status,
        "project": profile.get("project_key") or "unknown",
        "project_root": str(project_root),
        "work_root": str(work_root),
        "force": bool(args.force),
        "release_prune": release_prune,
        "global_release_prune": global_release_prune,
        "stale_run_cleanup": stale_cleanup,
        "artifact_projection": artifact_projection,
        "artifact_retention_plan": artifact_retention_plan,
    }
    if args.json:
        return summary
    _write_gc_text(summary)
    return 0


def _gc_artifact_retention_plan(work_root, limit, delete_artifacts=False):
    plan = read_projected_artifact_retention_plan(work_root, limit=limit)
    if plan is not None:
        projection_metadata = _projection_check_metadata(work_root)
        result = {
            **projection_metadata,
            **plan,
            "projection_db_path": plan.get("projection_db_path")
            or plan.get("db_path")
            or projection_metadata.get("projection_db_path"),
        }
        if delete_artifacts:
            result["artifact_deletion"] = _delete_rebuildable_artifact_candidates(result)
            result["deletion_enabled"] = True
            result["next_action"] = "run agentteam db rebuild"
            result["operator_hint"] = "Rebuild the projection DB after artifact deletion."
        return result
    projection_metadata = _projection_check_metadata(work_root)
    if delete_artifacts:
        raise AgentTeamCliError(
            "artifact deletion blocked",
            artifact_deletion_status="blocked",
            reason="fresh projection DB is required",
            next_action="run agentteam db rebuild",
        )
    return {
        **projection_metadata,
        "projection_source": "files",
        "plan_status": "unavailable",
        "deletion_enabled": False,
        "candidate_count": 0,
        "candidate_bytes": 0,
        "candidate_limit": max(0, int(limit if limit is not None else 20)),
        "retention_policies": {},
        "retention_bytes": {},
        "protected_explanations": [
            {
                "retention_policy": "unindexed",
                "reason": "artifact retention planning requires a fresh projection DB; run agentteam db rebuild first.",
            }
        ],
        "candidates": [],
    }


def _delete_rebuildable_artifact_candidates(plan):
    if plan.get("plan_status") != "ready":
        raise AgentTeamCliError(
            "artifact deletion blocked",
            artifact_deletion_status="blocked",
            reason="retention plan is not ready",
        )
    if plan.get("validation_status") != "passed":
        raise AgentTeamCliError(
            "artifact deletion blocked",
            artifact_deletion_status="blocked",
            reason="candidate validation failed",
            invalid_candidate_count=plan.get("invalid_candidate_count", 0),
            invalid_candidates=plan.get("invalid_candidates", []),
        )
    candidates = plan.get("candidates") if isinstance(plan.get("candidates"), list) else []
    blocked = [
        candidate
        for candidate in candidates
        if not isinstance(candidate, dict)
        or candidate.get("retention_policy") != "rebuildable"
        or not isinstance(candidate.get("validation"), dict)
        or candidate["validation"].get("status") != "passed"
    ]
    if blocked:
        raise AgentTeamCliError(
            "artifact deletion blocked",
            artifact_deletion_status="blocked",
            reason="candidate list contains non-rebuildable or unvalidated artifacts",
            blocked_candidate_count=len(blocked),
        )
    deleted = []
    skipped = []
    for candidate in candidates:
        path = Path(candidate["path"])
        if not path.is_file():
            skipped.append({"path": str(path), "reason": "missing"})
            continue
        size_bytes = path.stat().st_size
        path.unlink()
        deleted.append(
            {
                "path": str(path),
                "artifact_type": candidate.get("artifact_type"),
                "run_id": candidate.get("run_id"),
                "size_bytes": size_bytes,
            }
        )
    return {
        "deletion_status": "completed",
        "deleted_count": len(deleted),
        "deleted_bytes": sum(item["size_bytes"] for item in deleted),
        "deleted": deleted,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "post_delete_next_action": "run agentteam db rebuild",
    }


def _gc_artifact_projection_summary(work_root):
    projected = read_projected_artifact_summary(work_root)
    if projected is None:
        projection_metadata = _projection_check_metadata(work_root)
        return {
            **projection_metadata,
            "projection_source": "files",
            "total_artifacts": 0,
            "total_bytes": 0,
            "artifact_types": {},
            "retention_policies": {
                "authoritative": 0,
                "protected": 0,
                "rebuildable": 0,
            },
            "retention_bytes": {
                "authoritative": 0,
                "protected": 0,
                "rebuildable": 0,
            },
            "dry_run_explanations": [
                {
                    "retention_policy": "unindexed",
                    "reason": "artifact projection is missing, stale, or unreadable; run agentteam db rebuild for artifact-level cleanup explanations.",
                }
            ],
        }
    retention_policies = {
        "authoritative": 0,
        "protected": 0,
        "rebuildable": 0,
        **(projected.get("retention_policies") or {}),
    }
    retention_bytes = {
        "authoritative": 0,
        "protected": 0,
        "rebuildable": 0,
        **(projected.get("retention_bytes") or {}),
    }
    return {
        **_projection_output_metadata(projected),
        "projection_source": "db",
        "db_path": projected.get("db_path"),
        "check_status": projected.get("check_status"),
        "total_artifacts": projected.get("total_artifacts", 0),
        "total_bytes": projected.get("total_bytes", 0),
        "artifact_types": projected.get("artifact_types") or {},
        "retention_policies": retention_policies,
        "retention_bytes": retention_bytes,
        "run_token_usage": projected.get("run_token_usage") or [],
        "dry_run_explanations": _gc_artifact_dry_run_explanations(retention_policies),
    }


def _gc_artifact_dry_run_explanations(retention_policies):
    explanations = [
        {
            "retention_policy": "authoritative",
            "artifact_count": retention_policies.get("authoritative", 0),
            "reason": "authoritative audit artifacts such as events, state, reports, patches, and frozen taskpacks are not removed by gc.",
        },
        {
            "retention_policy": "protected",
            "artifact_count": retention_policies.get("protected", 0),
            "reason": "protected artifacts are tied to active or nonterminal run state and are not cleanup candidates.",
        },
        {
            "retention_policy": "rebuildable",
            "artifact_count": retention_policies.get("rebuildable", 0),
            "reason": "rebuildable derived artifacts such as role and repo contexts may become cleanup candidates after explicit retention policy support.",
        },
    ]
    return [
        explanation
        for explanation in explanations
        if explanation["artifact_count"] > 0
        or explanation["retention_policy"] in {"authoritative", "rebuildable"}
    ]


def _handle_db(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    if args.db_command == "rebuild":
        summary = rebuild_project_projection_db(work_root)
    elif args.db_command == "check":
        summary = check_project_projection_db(work_root)
    else:
        raise AgentTeamCliError("unsupported db command", db_command=args.db_command)
    summary = {
        **summary,
        "project": profile.get("project_key") or "unknown",
        "project_root": str(project_root),
        "work_root": str(work_root),
    }
    if args.json:
        return summary
    _write_db_text(summary)
    return 0


def _handle_stats(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    work_root = Path(profile["work_root"]).resolve()
    summary = {
        **build_project_stats(work_root),
        "project": profile.get("project_key") or "unknown",
        "project_root": str(project_root),
        "work_root": str(work_root),
    }
    if args.json:
        return summary
    _write_stats_text(summary)
    return 0


def _watch_profile(args):
    if args.run_dir:
        project_root = Path(args.project_root or ".").resolve()
        profile, _work_root = _profile_and_work_root_for_args_run_dir(args, project_root)
        return profile
    if args.project_root:
        return load_project_profile(Path(args.project_root).resolve())
    return load_project_profile(Path(".").resolve())


def _watch_run_dir(args, profile):
    if args.run_dir:
        return _canonical_run_dir(Path(args.run_dir).resolve())
    return _selected_run_dir(args, profile, command_name="watch")


def _write_watch_line(summary, events, json_lines=False):
    event_type = events[-1].get("event_type") if events else None
    if json_lines:
        _print_json(
            {
                "run": summary["latest_run"],
                "status": summary["status"],
                "liveness_status": summary["liveness_status"],
                "tasks": summary["tasks"],
                "inflight": summary["inflight"],
                "manual_gates": summary["manual_gates"],
                "event_type": event_type,
                "run_dir": summary["run_dir"],
            },
            stream=sys.stdout,
        )
        return
    pieces = [
        f"run={summary['latest_run']}",
        f"status={summary['status']}",
        f"liveness={summary['liveness_status']}",
        f"tasks={summary['tasks']['done']}/{summary['tasks']['total']}",
        f"blocked={summary['tasks']['blocked']}",
        f"inflight={summary['inflight']['total']}",
        f"manual_gates={summary['manual_gates']}",
    ]
    if event_type:
        pieces.append(f"event={event_type}")
    sys.stdout.write(" ".join(pieces) + "\n")
    sys.stdout.flush()


def _progress_completion_report(enabled, report):
    if not enabled:
        return
    for line in concise_report_lines(report):
        _write_progress(line)


def _watch_should_stop(summary):
    status = summary.get("status")
    liveness_status = summary.get("liveness_status")
    if liveness_status in {"running-alive"}:
        return False
    return status in {"idle", "stopped", "completed", "failed"} or liveness_status == "running-stale"


def _write_update_text(summary):
    lines = [
        f"update_status: {summary['update_status']}",
    ]
    if summary.get("project"):
        lines.insert(0, f"project: {summary['project']}")
    active = summary.get("active_release") or {}
    lines.append(f"active_release: {active.get('release_id') or 'none'}")
    latest = summary.get("latest_installed_release") or {}
    lines.append(f"latest_installed_release: {latest.get('release_id') or 'unknown'}")
    if summary.get("active_is_latest") is not None:
        lines.append(f"active_is_latest: {str(bool(summary.get('active_is_latest'))).lower()}")
    if summary.get("run_id"):
        binding = summary.get("runtime_release_binding") or {}
        lines.extend(
            [
                f"adopted_run: {summary['run_id']}",
                f"creation_sequence: {summary.get('creation_sequence') or 'unknown'}",
                f"bound_release: {binding.get('release_id') or 'unknown'}",
                f"bound_source_commit: {binding.get('source_commit') or 'unknown'}",
            ]
        )
    known = summary.get("known_releases") or []
    lines.append("known_releases:")
    if known:
        lines.extend(
            f"  - {release.get('release_id') or 'unknown'}"
            for release in known
            if isinstance(release, dict)
        )
    else:
        lines.append("  none")
    prune = summary.get("release_prune") or {}
    deleted_release_ids = prune.get("deleted_release_ids") or []
    if deleted_release_ids:
        lines.append("pruned_releases:")
        lines.extend(f"  - {release_id}" for release_id in deleted_release_ids)
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _build_doctor_summary(project_root):
    checks = []
    profile = None
    profile_path = profile_path_for_project(project_root)
    git = _git_completed(project_root, ["rev-parse", "--is-inside-work-tree"], check=False)
    if git.returncode == 0 and git.stdout.strip() == "true":
        checks.append(_doctor_check("git_repository", "passed", "project root is inside a git repository"))
    else:
        checks.append(
            _doctor_check(
                "git_repository",
                "failed",
                "project root is not a git repository",
                stderr=git.stderr.strip(),
            )
        )

    try:
        profile = load_project_profile(project_root)
        checks.append(
            _doctor_check(
                "profile",
                "passed",
                "AgentTeam profile loaded",
                profile_path=str(profile_path),
                project=profile.get("project_key") or "unknown",
            )
        )
    except AgentTeamProfileError as exc:
        checks.append(
            _doctor_check(
                "profile",
                "failed",
                str(exc),
                profile_path=str(profile_path),
            )
        )

    if isinstance(profile, dict):
        work_root = Path(profile.get("work_root") or "").expanduser()
        work_root_status = "passed" if work_root.exists() else "warning"
        checks.append(
            _doctor_check(
                "work_root",
                work_root_status,
                "work root exists" if work_root.exists() else "work root will be created when needed",
                work_root=str(work_root),
            )
        )
        checks.append(_doctor_verification_check(profile))
        checks.append(_doctor_feishu_check(profile))
    else:
        checks.append(_doctor_check("verification_profile", "skipped", "profile is not available"))
        checks.append(_doctor_check("feishu", "skipped", "profile is not available"))

    codex_path = shutil.which("codex")
    checks.append(
        _doctor_check(
            "codex_cli",
            "passed" if codex_path else "warning",
            "codex CLI found" if codex_path else "codex CLI was not found on PATH",
            path=codex_path,
        )
    )
    counts = _doctor_status_counts(checks)
    return {
        "doctor_status": "failed" if counts["failed"] else "passed",
        "project_root": str(project_root),
        "profile_path": str(profile_path),
        "checks": checks,
        "counts": counts,
    }


def _doctor_verification_check(profile):
    verification = profile.get("verification_profile")
    if not isinstance(verification, dict):
        return _doctor_check("verification_profile", "failed", "verification profile is missing")
    correctness = verification.get("correctness") if isinstance(verification.get("correctness"), dict) else {}
    command = correctness.get("command")
    if not _is_command_list(command):
        return _doctor_check(
            "verification_profile",
            "failed",
            "correctness command is missing or invalid",
        )
    performance = verification.get("performance") if isinstance(verification.get("performance"), dict) else {}
    return _doctor_check(
        "verification_profile",
        "passed",
        "correctness verification command is configured",
        correctness_command=command,
        performance_command=performance.get("command"),
        metrics=performance.get("metrics") or [],
    )


def _doctor_feishu_check(profile):
    feishu = profile.get("feishu") if isinstance(profile.get("feishu"), dict) else {}
    if not feishu.get("enabled"):
        return _doctor_check("feishu", "skipped", "Feishu notifications are not enabled")
    webhook_env = feishu.get("webhook_env")
    if not webhook_env:
        return _doctor_check("feishu", "warning", "Feishu is enabled but webhook env is missing")
    return _doctor_check(
        "feishu",
        "passed" if os.environ.get(webhook_env) else "warning",
        "Feishu webhook env is set" if os.environ.get(webhook_env) else "Feishu webhook env is configured but unset",
        webhook_env=webhook_env,
        webhook_env_set=bool(os.environ.get(webhook_env)),
        signing_secret_env=feishu.get("signing_secret_env"),
    )


def _doctor_check(name, status, summary, **details):
    check = {
        "name": name,
        "status": status,
        "summary": summary,
    }
    for key, value in details.items():
        if value is not None:
            check[key] = value
    return check


def _doctor_status_counts(checks):
    counts = {"passed": 0, "warning": 0, "failed": 0, "skipped": 0}
    for check in checks:
        status = check.get("status")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _is_command_list(value):
    return isinstance(value, list) and bool(value) and all(isinstance(part, str) for part in value)


def _write_doctor_text(summary):
    lines = [
        f"doctor_status: {summary['doctor_status']}",
        f"project_root: {summary['project_root']}",
    ]
    for check in summary.get("checks") or []:
        lines.append(f"{check['name']}: {check['status']} - {check['summary']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


_INVOCATION_PROBE_TIMEOUT_SECONDS = 15.0
_INVOCATION_PROBE_CLEANUP_SECONDS = 3.0
_INVOCATION_PROBE_SUITABLE_KILL_MODES = {"control-group", "mixed"}
_INVOCATION_PROBE_HELPER_CODE = (
    "import pathlib,sys,time\n"
    "gate=pathlib.Path(sys.argv[1])\n"
    "deadline=time.monotonic()+float(sys.argv[2])\n"
    "while time.monotonic()<deadline:\n"
    "    if gate.exists():\n"
    "        raise SystemExit(0)\n"
    "    time.sleep(0.02)\n"
    "raise SystemExit(124)\n"
)


class _InvocationProbeFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _InvocationProbeHost:
    def is_linux(self):
        return sys.platform.startswith("linux")

    def pidfd_supported(self):
        return callable(getattr(os, "pidfd_open", None))

    def uid(self):
        return os.getuid()

    def executable(self):
        return sys.executable

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def run(self, command, timeout):
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )

    def read_text(self, path):
        return Path(path).read_text(encoding="utf-8")

    def make_state_dir(self):
        return Path(tempfile.mkdtemp(prefix="agentteam-invocation-probe-"))

    def release_helper(self, gate_path):
        Path(gate_path).touch(exist_ok=False)

    def remove_state_dir(self, state_dir):
        shutil.rmtree(state_dir)

    def open_pidfd(self, pid):
        return os.pidfd_open(pid, 0)

    def pidfd_exited(self, pidfd, timeout_seconds):
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        return bool(poller.poll(max(1, int(timeout_seconds * 1000))))

    def close_pidfd(self, pidfd):
        os.close(pidfd)


def _run_invocation_supervision_probe(timeout_seconds=None, host=None):
    host = host or _InvocationProbeHost()
    timeout_seconds = float(timeout_seconds or _INVOCATION_PROBE_TIMEOUT_SECONDS)
    summary = {
        "probe": "invocation_supervision",
        "status": "failed",
        "linux": False,
        "pidfd_open": False,
        "linger": False,
        "boot_id": None,
        "enclosing_kill_mode": None,
        "enclosing_identity_stable": False,
        "user_manager_identity_stable": False,
        "transient_unit_created": False,
        "unit_identity_verified": False,
        "pidfd_opened": False,
        "cgroup_drained": False,
        "unit_identity_queryable_after_exit": False,
        "cleanup_complete": False,
        "provider_calls": 0,
        "failure_code": None,
    }
    deadline = host.monotonic() + max(0.1, timeout_seconds)
    state_dir = None
    unit_name = f"agentteam-invocation-probe-{os.getpid()}-{uuid.uuid4().hex}.service"
    unit_cleanup_needed = False
    unit_control_group = None
    pidfd = None
    failure_code = None
    try:
        if not host.is_linux():
            raise _InvocationProbeFailure("linux_required")
        summary["linux"] = True
        if not host.pidfd_supported():
            raise _InvocationProbeFailure("pidfd_open_unavailable")
        summary["pidfd_open"] = True

        boot_id = host.read_text("/proc/sys/kernel/random/boot_id").strip()
        if not _is_uuid(boot_id):
            raise _InvocationProbeFailure("boot_id_missing")
        summary["boot_id"] = boot_id

        uid = host.uid()
        linger = _invocation_probe_command(
            host,
            ["loginctl", "show-user", str(uid), "--property=Linger", "--value"],
            deadline,
            "loginctl_failed",
        ).stdout.strip().lower()
        if linger != "yes":
            raise _InvocationProbeFailure("linger_disabled")
        summary["linger"] = True

        enclosing_unit = f"user@{uid}.service"
        enclosing_before = _invocation_probe_show_enclosing(host, enclosing_unit, deadline)
        manager_before = _invocation_probe_manager_identity(host, enclosing_before)
        summary["enclosing_kill_mode"] = enclosing_before["KillMode"]

        existing = _invocation_probe_show_unit(host, unit_name, deadline, allow_missing=True)
        if existing.get("LoadState") not in {None, "", "not-found"}:
            raise _InvocationProbeFailure("unexpected_unit_reuse")

        state_dir = host.make_state_dir()
        gate_path = state_dir / "release"
        unit_cleanup_needed = True
        start_command = [
            "systemd-run",
            "--user",
            "--quiet",
            "--no-block",
            f"--unit={unit_name}",
            "--property=Type=oneshot",
            "--property=RemainAfterExit=yes",
            "--property=KillMode=control-group",
            "--",
            host.executable(),
            "-c",
            _INVOCATION_PROBE_HELPER_CODE,
            str(gate_path),
            str(max(1.0, timeout_seconds)),
        ]
        _invocation_probe_command(
            host,
            start_command,
            deadline,
            "transient_unit_start_failed",
        )
        summary["transient_unit_created"] = True

        unit_before = _invocation_probe_wait_for_unit(host, unit_name, deadline)
        _invocation_probe_validate_unit(unit_before, unit_name)
        helper_pid = _positive_int(unit_before.get("MainPID"), "unit_identity_missing")
        helper_start_ticks = _invocation_probe_start_ticks(host, helper_pid)
        helper_cgroup = _invocation_probe_process_cgroup(host, helper_pid)
        unit_control_group = unit_before["ControlGroup"]
        if helper_cgroup != unit_control_group:
            raise _InvocationProbeFailure("helper_cgroup_mismatch")
        if not _is_descendant_cgroup(unit_control_group, enclosing_before["ControlGroup"]):
            raise _InvocationProbeFailure("helper_cgroup_mismatch")

        enclosing_during = _invocation_probe_show_enclosing(host, enclosing_unit, deadline)
        manager_during = _invocation_probe_manager_identity(host, enclosing_during)
        if not _same_enclosing_identity(enclosing_before, enclosing_during):
            raise _InvocationProbeFailure("enclosing_user_service_restarted")
        if manager_before != manager_during:
            raise _InvocationProbeFailure("user_manager_identity_changed")
        summary["enclosing_identity_stable"] = True
        summary["user_manager_identity_stable"] = True

        unit_crosscheck = _invocation_probe_show_unit(host, unit_name, deadline)
        _invocation_probe_validate_unit(unit_crosscheck, unit_name)
        if not _same_unit_identity(unit_before, unit_crosscheck):
            raise _InvocationProbeFailure("unit_identity_changed")
        if _invocation_probe_start_ticks(host, helper_pid) != helper_start_ticks:
            raise _InvocationProbeFailure("helper_identity_changed")
        summary["unit_identity_verified"] = True

        try:
            pidfd = host.open_pidfd(helper_pid)
        except OSError as exc:
            raise _InvocationProbeFailure("pidfd_open_failed") from exc
        summary["pidfd_opened"] = True
        host.release_helper(gate_path)
        if not host.pidfd_exited(pidfd, _invocation_probe_remaining(host, deadline)):
            raise _InvocationProbeFailure("helper_exit_timeout")

        _invocation_probe_wait_for_empty_cgroup(host, unit_control_group, deadline)
        summary["cgroup_drained"] = True
        unit_after = _invocation_probe_show_unit(host, unit_name, deadline)
        if not _same_queryable_unit_identity(unit_before, unit_after):
            raise _InvocationProbeFailure("unit_identity_not_queryable")
        summary["unit_identity_queryable_after_exit"] = True

        enclosing_after = _invocation_probe_show_enclosing(host, enclosing_unit, deadline)
        manager_after = _invocation_probe_manager_identity(host, enclosing_after)
        if not _same_enclosing_identity(enclosing_before, enclosing_after):
            raise _InvocationProbeFailure("enclosing_user_service_restarted")
        if manager_before != manager_after:
            raise _InvocationProbeFailure("user_manager_identity_changed")
    except _InvocationProbeFailure as exc:
        failure_code = exc.code
    except (OSError, ValueError):
        failure_code = "host_operation_failed"
    finally:
        cleanup_deadline = host.monotonic() + _INVOCATION_PROBE_CLEANUP_SECONDS
        cleanup_complete = True
        if pidfd is not None:
            try:
                host.close_pidfd(pidfd)
            except OSError:
                cleanup_complete = False
        if unit_cleanup_needed:
            cleanup_complete = (
                _invocation_probe_cleanup_unit(
                    host,
                    unit_name,
                    unit_control_group,
                    cleanup_deadline,
                )
                and cleanup_complete
            )
        if state_dir is not None:
            try:
                host.remove_state_dir(state_dir)
            except OSError:
                cleanup_complete = False
        summary["cleanup_complete"] = cleanup_complete
        if not cleanup_complete and failure_code is None:
            failure_code = "cleanup_incomplete"

    summary["failure_code"] = failure_code
    summary["status"] = "passed" if failure_code is None else "failed"
    return summary


def _invocation_probe_command(host, command, deadline, failure_code, allow_failure=False):
    try:
        completed = host.run(command, _invocation_probe_remaining(host, deadline))
    except subprocess.TimeoutExpired as exc:
        raise _InvocationProbeFailure("timeout") from exc
    except FileNotFoundError as exc:
        raise _InvocationProbeFailure("required_command_unavailable") from exc
    except OSError as exc:
        raise _InvocationProbeFailure("host_operation_failed") from exc
    if completed.returncode != 0 and not allow_failure:
        raise _InvocationProbeFailure(failure_code)
    return completed


def _invocation_probe_remaining(host, deadline):
    remaining = deadline - host.monotonic()
    if remaining <= 0:
        raise _InvocationProbeFailure("timeout")
    return max(0.01, remaining)


def _invocation_probe_show_enclosing(host, unit_name, deadline):
    completed = _invocation_probe_command(
        host,
        [
            "systemctl",
            "show",
            unit_name,
            "--no-page",
            "--property=Id",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=KillMode",
            "--property=MainPID",
        ],
        deadline,
        "enclosing_user_service_unavailable",
    )
    properties = _invocation_probe_properties(completed.stdout)
    if properties.get("Id") != unit_name:
        raise _InvocationProbeFailure("enclosing_identity_missing")
    if not _is_invocation_id(properties.get("InvocationID")):
        raise _InvocationProbeFailure("enclosing_identity_missing")
    if not _is_absolute_cgroup(properties.get("ControlGroup")):
        raise _InvocationProbeFailure("enclosing_identity_missing")
    if properties.get("KillMode") not in _INVOCATION_PROBE_SUITABLE_KILL_MODES:
        raise _InvocationProbeFailure("enclosing_kill_mode_unsuitable")
    _positive_int(properties.get("MainPID"), "user_manager_identity_missing")
    return properties


def _invocation_probe_manager_identity(host, enclosing):
    manager_pid = _positive_int(enclosing.get("MainPID"), "user_manager_identity_missing")
    start_ticks = _invocation_probe_start_ticks(host, manager_pid)
    process_cgroup = _invocation_probe_process_cgroup(host, manager_pid)
    if (
        process_cgroup != enclosing["ControlGroup"]
        and not _is_descendant_cgroup(process_cgroup, enclosing["ControlGroup"])
    ):
        raise _InvocationProbeFailure("user_manager_identity_missing")
    return manager_pid, start_ticks, process_cgroup


def _invocation_probe_show_unit(host, unit_name, deadline, allow_missing=False):
    completed = _invocation_probe_command(
        host,
        [
            "systemctl",
            "--user",
            "show",
            unit_name,
            "--no-page",
            "--property=Id",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            "--property=ActiveState",
            "--property=SubState",
            "--property=LoadState",
            "--property=KillMode",
            "--property=RemainAfterExit",
            "--property=Type",
        ],
        deadline,
        "unit_identity_unavailable",
        allow_failure=allow_missing,
    )
    if completed.returncode != 0 and allow_missing:
        return {"LoadState": "not-found"}
    return _invocation_probe_properties(completed.stdout)


def _invocation_probe_wait_for_unit(host, unit_name, deadline):
    while True:
        properties = _invocation_probe_show_unit(host, unit_name, deadline, allow_missing=True)
        if properties.get("LoadState") == "loaded" and _int_or_zero(properties.get("MainPID")) > 0:
            return properties
        if properties.get("LoadState") not in {None, "", "not-found", "loaded"}:
            raise _InvocationProbeFailure("unit_identity_missing")
        host.sleep(min(0.02, _invocation_probe_remaining(host, deadline)))


def _invocation_probe_validate_unit(properties, unit_name):
    if properties.get("Id") != unit_name:
        raise _InvocationProbeFailure("unit_identity_missing")
    if not _is_invocation_id(properties.get("InvocationID")):
        raise _InvocationProbeFailure("unit_identity_missing")
    if not _is_absolute_cgroup(properties.get("ControlGroup")):
        raise _InvocationProbeFailure("unit_identity_missing")
    if properties.get("KillMode") != "control-group":
        raise _InvocationProbeFailure("unit_property_mismatch")
    if properties.get("RemainAfterExit") != "yes":
        raise _InvocationProbeFailure("unit_property_mismatch")
    if properties.get("Type") != "oneshot":
        raise _InvocationProbeFailure("unit_property_mismatch")
    _positive_int(properties.get("MainPID"), "unit_identity_missing")


def _invocation_probe_start_ticks(host, pid):
    try:
        stat = host.read_text(f"/proc/{pid}/stat").strip()
    except OSError as exc:
        raise _InvocationProbeFailure("process_identity_missing") from exc
    closing = stat.rfind(")")
    fields = stat[closing + 1 :].split() if closing >= 0 else []
    if len(fields) < 20:
        raise _InvocationProbeFailure("process_identity_missing")
    return _positive_int(fields[19], "process_identity_missing")


def _invocation_probe_process_cgroup(host, pid):
    try:
        lines = host.read_text(f"/proc/{pid}/cgroup").splitlines()
    except OSError as exc:
        raise _InvocationProbeFailure("process_identity_missing") from exc
    unified = [line.split("::", 1)[1] for line in lines if line.startswith("0::")]
    if len(unified) != 1 or not _is_absolute_cgroup(unified[0]):
        raise _InvocationProbeFailure("process_identity_missing")
    return unified[0]


def _invocation_probe_wait_for_empty_cgroup(host, control_group, deadline):
    events_path = _invocation_probe_cgroup_events_path(control_group)
    while True:
        try:
            events = _invocation_probe_properties(host.read_text(events_path), separator=" ")
        except OSError as exc:
            raise _InvocationProbeFailure("cgroup_events_unavailable") from exc
        if events.get("populated") == "0":
            return
        if events.get("populated") != "1":
            raise _InvocationProbeFailure("cgroup_events_invalid")
        host.sleep(min(0.02, _invocation_probe_remaining(host, deadline)))


def _invocation_probe_cleanup_unit(host, unit_name, control_group, deadline):
    cleanup_ok = True
    for action in ("stop", "reset-failed"):
        try:
            _invocation_probe_command(
                host,
                ["systemctl", "--user", action, unit_name],
                deadline,
                "cleanup_incomplete",
                allow_failure=True,
            )
        except _InvocationProbeFailure:
            cleanup_ok = False
    while cleanup_ok:
        try:
            properties = _invocation_probe_show_unit(host, unit_name, deadline, allow_missing=True)
        except _InvocationProbeFailure:
            cleanup_ok = False
            break
        if properties.get("LoadState") in {None, "", "not-found"}:
            break
        try:
            host.sleep(min(0.02, _invocation_probe_remaining(host, deadline)))
        except _InvocationProbeFailure:
            cleanup_ok = False
    if control_group:
        try:
            events = _invocation_probe_properties(
                host.read_text(_invocation_probe_cgroup_events_path(control_group)),
                separator=" ",
            )
            cleanup_ok = events.get("populated") == "0" and cleanup_ok
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_ok = False
    return cleanup_ok


def _invocation_probe_properties(output, separator="="):
    properties = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or separator not in line:
            continue
        key, value = line.split(separator, 1)
        properties[key.strip()] = value.strip()
    return properties


def _invocation_probe_cgroup_events_path(control_group):
    relative = Path(control_group.lstrip("/"))
    if ".." in relative.parts:
        raise _InvocationProbeFailure("helper_cgroup_mismatch")
    return Path("/sys/fs/cgroup") / relative / "cgroup.events"


def _is_uuid(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError):
        return False


def _is_invocation_id(value):
    if not isinstance(value, str) or len(value) != 32:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _is_absolute_cgroup(value):
    return isinstance(value, str) and value.startswith("/") and ".." not in Path(value).parts


def _is_descendant_cgroup(child, parent):
    parent = parent.rstrip("/")
    return child.startswith(parent + "/")


def _positive_int(value, failure_code):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _InvocationProbeFailure(failure_code) from exc
    if parsed <= 0:
        raise _InvocationProbeFailure(failure_code)
    return parsed


def _int_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _same_enclosing_identity(left, right):
    keys = ("Id", "InvocationID", "ControlGroup", "KillMode", "MainPID")
    return all(left.get(key) == right.get(key) for key in keys)


def _same_unit_identity(left, right):
    keys = ("Id", "InvocationID", "ControlGroup", "MainPID", "KillMode", "RemainAfterExit", "Type")
    return all(left.get(key) == right.get(key) for key in keys)


def _same_queryable_unit_identity(left, right):
    keys = ("Id", "InvocationID", "KillMode", "RemainAfterExit", "Type")
    return (
        all(left.get(key) == right.get(key) for key in keys)
        and right.get("LoadState") == "loaded"
        and right.get("ActiveState") == "active"
        and right.get("SubState") == "exited"
    )


def _write_invocation_supervision_probe_text(summary):
    lines = [
        f"invocation_supervision_probe: {summary['status']}",
        f"linux: {str(summary['linux']).lower()}",
        f"pidfd_open: {str(summary['pidfd_open']).lower()}",
        f"linger: {str(summary['linger']).lower()}",
        f"enclosing_kill_mode: {summary.get('enclosing_kill_mode') or 'unknown'}",
        f"identity_stable: {str(summary['enclosing_identity_stable'] and summary['user_manager_identity_stable']).lower()}",
        f"cgroup_drained: {str(summary['cgroup_drained']).lower()}",
        f"cleanup_complete: {str(summary['cleanup_complete']).lower()}",
        f"provider_calls: {summary['provider_calls']}",
    ]
    if summary.get("failure_code"):
        lines.append(f"failure_code: {summary['failure_code']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _projection_text_lines(summary):
    if not isinstance(summary, dict):
        return []
    lines = []
    if summary.get("projection_source"):
        lines.append(f"projection_source: {summary['projection_source']}")
    if summary.get("projection_status"):
        lines.append(f"projection_status: {summary['projection_status']}")
    if summary.get("projection_warning"):
        lines.append(f"projection_warning: {summary['projection_warning']}")
    if summary.get("projection_next_action"):
        lines.append(f"projection_next_action: {summary['projection_next_action']}")
    if summary.get("projection_operator_hint"):
        lines.append(f"projection_operator_hint: {summary['projection_operator_hint']}")
    if summary.get("next_action"):
        lines.append(f"next_action: {summary['next_action']}")
    if summary.get("operator_hint"):
        lines.append(f"operator_hint: {summary['operator_hint']}")
    return lines


def _write_logs_text(summary):
    lines = [
        f"run: {summary['latest_run']}",
        f"events: {summary['returned_count']}/{summary['event_count']}",
        *_projection_text_lines(summary),
        f"run_dir: {summary['run_dir']}",
    ]
    lines.extend(_format_log_event(event) for event in summary.get("events") or [])
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _format_log_event(event):
    event_id = event.get("event_id") or f"seq-{event.get('sequence', 'unknown')}"
    event_type = event.get("event_type") or "unknown"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    pieces = [str(event_id), str(event_type)]
    for key in ("run_status", "scheduler_status", "task_id", "attempt_id"):
        if payload.get(key):
            pieces.append(f"{key}={payload[key]}")
    return " ".join(pieces)


def _write_explain_status_text(summary):
    lines = [
        f"project: {summary['project']}",
        f"latest_run: {summary['latest_run']}",
        f"overall_status: {summary['overall_status']}",
        f"run_status: {summary['run_status']}",
        f"liveness: {summary['liveness_status']}",
        f"Explanation: {summary['explanation']}",
        f"Next action: {summary['next_action']}",
        f"run_dir: {summary['run_dir']}",
    ]
    for request in summary.get("permission_request_details") or []:
        lines.append(
            "permission_request: "
            f"{request.get('request_id') or 'unknown'} "
            f"task={request.get('task_id') or 'unknown'} "
            f"capability={request.get('requested_capability') or 'runtime_permission'}"
        )
        if request.get("approve_command"):
            lines.append(f"approve: {request['approve_command']}")
        if request.get("deny_command"):
            lines.append(f"deny: {request['deny_command']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _status_explanation(summary):
    overall_status = summary.get("overall_status") or summary.get("status")
    if overall_status == "authoring":
        return "taskpack authoring is currently active."
    if overall_status == "manual_gate_required":
        return "runtime is paused waiting for an operator answer."
    if overall_status == "permission_required":
        return "runtime is paused waiting for a permission decision."
    if overall_status == "running":
        return "at least one worker or runtime process is currently active."
    if overall_status == "idle":
        return "no worker or authoring process is currently active."
    if overall_status == "blocked":
        return "one or more tasks are blocked and need review before progress can continue."
    if overall_status == "failed":
        return "the run failed; inspect the report and recent logs before retrying."
    return "status is available, but no specialized explanation is defined for it."


def _status_next_action(summary):
    overall_status = summary.get("overall_status") or summary.get("status")
    if overall_status == "authoring":
        return "wait for taskpack authoring to finish, or stop authoring if it is stale."
    if overall_status == "manual_gate_required":
        return "run agentteam resume --run-dir <run> --interactive."
    if overall_status == "permission_required":
        return "run agentteam permissions list --run-dir <run>."
    if overall_status == "running":
        return "watch progress or wait for the next state change."
    if overall_status == "idle":
        return "review the report, integrate accepted changes, or start a follow-up task."
    if overall_status == "blocked":
        return "inspect agentteam report and decide whether to retry, narrow scope, or change the task."
    if overall_status == "failed":
        return "inspect agentteam logs and report before continuing."
    return "inspect agentteam status, logs, and report for details."


def _write_gc_text(summary):
    prune = summary.get("release_prune") or {}
    lines = [
        f"gc_status: {summary['gc_status']}",
        f"project: {summary['project']}",
        f"work_root: {summary['work_root']}",
        f"release_prune: {prune.get('prune_status') or 'unknown'}",
        f"deleted_releases: {len(prune.get('deleted_release_ids') or [])}",
    ]
    if prune.get("force_required"):
        lines.append("force_required: true")
    global_prune = summary.get("global_release_prune") or {}
    if global_prune:
        lines.append(f"global_release_prune: {global_prune.get('prune_status') or 'unknown'}")
        lines.append(f"global_protected_releases: {len(global_prune.get('protected_global_release_ids') or [])}")
        lines.append(f"global_deletable_releases: {len(global_prune.get('deletable_global_release_ids') or [])}")
        lines.append(f"global_deleted_releases: {len(global_prune.get('deleted_global_release_ids') or [])}")
        if global_prune.get("force_required"):
            lines.append("global_force_required: true")
    if summary.get("stale_run_cleanup"):
        cleanup = summary["stale_run_cleanup"]
        lines.append(f"stale_run_cleanup: {cleanup.get('cleanup_status') or cleanup.get('stop_status') or 'completed'}")
    artifact_projection = summary.get("artifact_projection") or {}
    if artifact_projection:
        lines.append(f"artifact_projection: {artifact_projection.get('projection_source') or 'unknown'}")
        if artifact_projection.get("projection_status"):
            lines.append(f"artifact_projection_status: {artifact_projection['projection_status']}")
        if artifact_projection.get("projection_warning"):
            lines.append(f"artifact_projection_warning: {artifact_projection['projection_warning']}")
        if artifact_projection.get("next_action"):
            lines.append(f"artifact_projection_next_action: {artifact_projection['next_action']}")
        if artifact_projection.get("operator_hint"):
            lines.append(f"artifact_projection_operator_hint: {artifact_projection['operator_hint']}")
        lines.append(f"artifact_count: {artifact_projection.get('total_artifacts', 0)}")
        lines.append(f"artifact_bytes: {artifact_projection.get('total_bytes', 0)}")
    artifact_plan = summary.get("artifact_retention_plan") or {}
    if artifact_plan:
        lines.append(f"artifact_retention_plan: {artifact_plan.get('plan_status') or 'unknown'}")
        if artifact_plan.get("projection_source"):
            lines.append(f"artifact_retention_plan_projection_source: {artifact_plan['projection_source']}")
        if artifact_plan.get("projection_status"):
            lines.append(f"artifact_retention_plan_status: {artifact_plan['projection_status']}")
        if artifact_plan.get("projection_warning"):
            lines.append(f"artifact_retention_plan_warning: {artifact_plan['projection_warning']}")
        if artifact_plan.get("next_action"):
            lines.append(f"artifact_retention_plan_next_action: {artifact_plan['next_action']}")
        if artifact_plan.get("operator_hint"):
            lines.append(f"artifact_retention_plan_operator_hint: {artifact_plan['operator_hint']}")
        lines.append(f"artifact_candidates: {artifact_plan.get('candidate_count', 0)}")
        lines.append(f"artifact_deletion_enabled: {str(bool(artifact_plan.get('deletion_enabled'))).lower()}")
        deletion = artifact_plan.get("artifact_deletion") if isinstance(artifact_plan.get("artifact_deletion"), dict) else {}
        if deletion:
            lines.append(f"artifact_deletion: {deletion.get('deletion_status') or 'unknown'}")
            lines.append(f"artifact_deleted_count: {deletion.get('deleted_count', 0)}")
            lines.append(f"artifact_deleted_bytes: {deletion.get('deleted_bytes', 0)}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_db_text(summary):
    status = summary.get("db_status") or summary.get("check_status") or "unknown"
    lines = [
        f"db_status: {status}",
        f"project: {summary.get('project') or 'unknown'}",
        f"db_path: {summary.get('db_path') or 'unknown'}",
        f"runs: {summary.get('runs', summary.get('actual', {}).get('runs', 0))}",
        f"taskpacks: {summary.get('taskpacks', summary.get('actual', {}).get('taskpacks', 0))}",
        f"events: {summary.get('events', summary.get('actual', {}).get('events', 0))}",
        f"tasks: {summary.get('tasks', summary.get('actual', {}).get('tasks', 0))}",
        f"evidence: {_format_projection_evidence(summary)}",
    ]
    mismatches = summary.get("mismatches")
    if mismatches:
        lines.append(f"mismatches: {', '.join(mismatches)}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_stats_text(summary):
    artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
    token_usage = summary.get("token_usage") if isinstance(summary.get("token_usage"), dict) else {}
    lines = [
        f"stats_status: {summary.get('stats_status') or 'unknown'}",
        f"project: {summary.get('project') or 'unknown'}",
        f"projection_source: {summary.get('projection_source') or 'unknown'}",
        f"check_status: {summary.get('check_status') or 'unknown'}",
        f"runs: {summary.get('runs', 0)}",
        f"taskpacks: {summary.get('taskpacks', 0)}",
        f"events: {summary.get('events', 0)}",
        f"tasks: {summary.get('tasks', 0)}",
        f"evidence: {_format_projection_evidence(summary)}",
        f"artifacts: {artifacts.get('total_count', 0)}",
        f"artifact_bytes: {artifacts.get('total_bytes', 0)}",
        format_token_usage(token_usage, label="tokens"),
    ]
    if summary.get("next_action"):
        lines.append(f"next_action: {summary['next_action']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _format_projection_evidence(summary):
    evidence = summary.get("evidence")
    if not isinstance(evidence, dict):
        actual = summary.get("actual") if isinstance(summary.get("actual"), dict) else {}
        evidence = actual.get("evidence") if isinstance(actual.get("evidence"), dict) else {}
    parts = [
        f"{status}={evidence[status]}"
        for status in sorted(evidence)
        if evidence.get(status)
    ]
    return ", ".join(parts) if parts else "none"


def _attach_release_status_fields(summary, profile):
    status = update_status(profile)
    enriched = dict(summary)
    for key in (
        "active_release",
        "latest_installed_release",
        "active_is_latest",
        "known_releases",
        "run_staging",
    ):
        if key not in enriched or enriched[key] is None:
            enriched[key] = status.get(key)
    return enriched


def _record_run_release(run_dir, profile):
    work_root = profile.get("work_root") if isinstance(profile, dict) else None
    if not work_root:
        return {"recorded": False, "reason": "missing_work_root"}
    try:
        return record_active_release_for_run(run_dir, work_root)
    except Exception as exc:
        return {
            "recorded": False,
            "reason": "record_failed",
            "error": str(exc),
        }


def _selected_run_dir(args, profile, command_name):
    work_root = Path(profile["work_root"]).resolve()
    if args.taskpack:
        taskpack_id = args.taskpack
    elif args.run_dir:
        taskpack_id = Path(args.run_dir).resolve().name
    else:
        taskpack_id = _latest_run_dir(profile).name
    if not taskpack_id:
        raise AgentTeamCliError(f"taskpack id is required for {command_name}")
    if args.run_dir and Path(args.run_dir).resolve().name != taskpack_id:
        raise AgentTeamCliError(
            "run directory name must match taskpack id",
            taskpack_id=taskpack_id,
            run_dir=str(Path(args.run_dir).resolve()),
        )
    selected = Path(args.run_dir).resolve() if args.run_dir else (work_root / "runs" / taskpack_id).resolve()
    return _canonical_run_dir(selected)


def _write_stop_text(summary):
    lines = [
        f"project: {summary.get('project') or 'unknown'}",
        f"stop_status: {summary['stop_status']}",
    ]
    if "latest_run" in summary:
        lines.insert(1, f"latest_run: {summary['latest_run']}")
    workers = summary.get("workers")
    if isinstance(workers, dict):
        lines.append(
            "workers: "
            f"{workers.get('stopped', 0)} stopped, "
            f"{workers.get('stop_requested', 0)} stop_requested, "
            f"{workers.get('running', 0)} running"
        )
    if summary.get("taskpack_id"):
        lines.append(f"taskpack_id: {summary['taskpack_id']}")
    if summary.get("pid"):
        lines.append(f"pid: {summary['pid']}")
    if summary.get("state_path"):
        lines.append(f"author_state: {summary['state_path']}")
    if summary.get("run_dir"):
        lines.append(f"run_dir: {summary['run_dir']}")
    if summary.get("cleaned_count") is not None:
        lines.append(f"cleaned_runs: {summary['cleaned_count']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _latest_run_dir(profile):
    work_root = profile.get("work_root")
    if not work_root:
        raise AgentTeamCliError("profile is missing work_root")
    selection = _launcher_runtime_selection()
    if selection and selection.get("selection_mode") == "implicit_latest":
        selected = Path(selection["run_dir"]).resolve()
        pair = validate_run_binding(
            selected,
            expected_project_key=selection.get("project_key"),
            expected_identity_sha256=selection.get("identity_sha256"),
        )
        return Path(pair["run_dir"])
    run_root = Path(work_root) / "runs"
    if not run_root.exists():
        raise AgentTeamCliError("no AgentTeam runs found", run_root=str(run_root))
    run_dirs = [path for path in run_root.iterdir() if path.is_dir()]
    if not run_dirs:
        raise AgentTeamCliError("no AgentTeam runs found", run_root=str(run_root))
    return _canonical_run_dir(max(run_dirs, key=lambda path: path.stat().st_mtime))


def _build_project_status_summary(profile, authoring):
    active_authoring = _active_authoring_record(authoring)
    return {
        "status_scope": "project",
        "project": profile.get("project_key") or "unknown",
        "status": "authoring",
        "overall_status": "authoring",
        "run_status": None,
        "active_phase": "authoring",
        "active_authoring": active_authoring,
        "authoring": authoring,
        "work_root": profile.get("work_root"),
    }


def _build_project_authoring_summary(profile):
    records = _authoring_state_records(profile)
    active = [
        record
        for record in records
        if record.get("liveness_status") == "running-alive"
    ]
    latest = records[-1] if records else None
    return {
        "total_count": len(records),
        "active_count": len(active),
        "latest": latest,
        "active": active,
    }


def _active_authoring_record(authoring):
    if not isinstance(authoring, dict) or not authoring.get("active_count"):
        return None
    active = authoring.get("active")
    if isinstance(active, list) and active:
        return active[-1]
    latest = authoring.get("latest")
    if isinstance(latest, dict) and latest.get("liveness_status") == "running-alive":
        return latest
    return None


def _overall_run_status(run_status, liveness_status, manual_gate_count, permission_request_count, authoring):
    if _active_authoring_record(authoring):
        return "authoring"
    if manual_gate_count:
        return "manual_gate_required"
    if permission_request_count:
        return "permission_required"
    if liveness_status == "running-alive":
        return "running"
    return run_status or "unknown"


def _status_run_outcome(run_status, task_counts):
    blocked_count = task_counts.get("blocked", 0) if isinstance(task_counts, dict) else 0
    if blocked_count and run_status in {"completed", "idle", "stopped"}:
        return "completed_with_review_required"
    if blocked_count:
        return "review_required"
    return run_status or "unknown"


def _active_phase_for_status(overall_status):
    phase_by_status = {
        "authoring": "authoring",
        "manual_gate_required": "manual_gate",
        "permission_required": "permission_request",
        "running": "runtime",
    }
    return phase_by_status.get(overall_status)


def _authoring_state_records(profile):
    work_root = profile.get("work_root")
    if not work_root:
        return []
    drafts_root = Path(work_root).resolve() / "drafts"
    if not drafts_root.exists():
        return []
    records = []
    for state_path in sorted(drafts_root.glob(".*-author/author_state.json")):
        state = _read_json_if_exists(state_path)
        if not isinstance(state, dict) or not state:
            continue
        record = {
            **state,
            "state_path": str(state_path.resolve()),
            "author_context_dir": str(state_path.parent.resolve()),
            "liveness_status": _author_liveness_status(state),
        }
        records.append(record)
    return sorted(records, key=lambda item: item.get("updated_at") or item.get("started_at") or "")


def _author_liveness_status(state):
    if not isinstance(state, dict):
        return "unknown"
    status = state.get("author_status")
    pid = state.get("pid")
    if status == "running":
        return "running-alive" if _pid_is_running(pid) else "running-stale"
    if status in {"completed", "failed", "timed_out", "stopped"}:
        return "not-running"
    return "unknown"


def _stop_authoring(profile, grace_seconds=5, force=False, operator="operator"):
    summary = _build_project_authoring_summary(profile)
    active = summary.get("active") or []
    if not active:
        latest = summary.get("latest") or {}
        return {
            "stop_status": "no_running_authoring",
            "authoring": summary,
            "latest_authoring": latest.get("taskpack_id"),
        }
    target = active[-1]
    pid = target.get("pid")
    state_path = Path(target["state_path"])
    stopped = False
    stop_error = None
    stop_signal = "SIGTERM"
    try:
        os.kill(int(pid), signal.SIGTERM)
        deadline = time.monotonic() + max(float(grace_seconds or 0), 0.0)
        while time.monotonic() < deadline:
            if not _pid_is_running(pid):
                stopped = True
                break
            time.sleep(0.1)
        if not stopped and force:
            stop_signal = "SIGKILL"
            os.kill(int(pid), signal.SIGKILL)
            stopped = not _pid_is_running(pid)
    except (OSError, ProcessLookupError, PermissionError, TypeError, ValueError) as exc:
        stop_error = str(exc)
        stopped = not _pid_is_running(pid)
    final_state = {
        **target,
        "author_status": "stopped" if stopped else "stop_requested",
        "stopped_by": operator,
        "stopped_at": _format_utc_timestamp(datetime.now(UTC)),
        "stop_signal": stop_signal,
    }
    if stop_error:
        final_state["stop_error"] = stop_error
    _write_json(state_path, final_state)
    return {
        "stop_status": "stopped_authoring" if stopped else "stop_requested",
        "taskpack_id": target.get("taskpack_id"),
        "pid": pid,
        "state_path": str(state_path),
        "authoring": _build_project_authoring_summary(profile),
    }


def _pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            parts = proc_stat.read_text(encoding="utf-8").split()
            if len(parts) > 2 and parts[2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _canonical_run_dir(run_dir):
    run_dir = Path(run_dir).resolve()
    nested = (run_dir / run_dir.name).resolve()
    if not _run_dir_has_runtime_artifacts(run_dir) and _run_dir_has_runtime_artifacts(nested):
        return nested
    return run_dir


def _run_dir_has_runtime_artifacts(run_dir):
    run_dir = Path(run_dir)
    return (
        (run_dir / "events.jsonl").exists()
        or (run_dir / "state" / "two_phase_scheduler_state.json").exists()
        or (run_dir / "state" / "scheduler_state.json").exists()
    )


def _run_paths_for_frozen_taskpack(frozen_taskpack_dir, run_root):
    frozen_taskpack_dir = Path(frozen_taskpack_dir).resolve()
    loaded = load_taskpack(frozen_taskpack_dir)
    taskpack_id = loaded["taskpack"].get("taskpack_id") or frozen_taskpack_dir.name
    supplied_run_root = Path(run_root).resolve()
    normalized = supplied_run_root.name == taskpack_id
    actual_run_root = supplied_run_root.parent.resolve() if normalized else supplied_run_root
    run_dir = (actual_run_root / taskpack_id).resolve()
    return {
        "taskpack_id": taskpack_id,
        "supplied_run_root": supplied_run_root,
        "run_root": actual_run_root,
        "run_dir": run_dir,
        "normalized_from_concrete_run_dir": normalized,
    }


def _build_run_status_summary(profile, run_dir):
    run_dir = Path(run_dir).resolve()
    events_path = run_dir / "events.jsonl"
    work_root = Path(profile.get("work_root") or run_dir.parent.parent).resolve()
    projected_events = read_projected_run_events(
        work_root,
        run_dir.name,
        include_fallback_status=True,
    )
    projection_metadata = _projection_output_metadata(projected_events)
    if projected_events is not None and projected_events.get("projection_source") == "db":
        snapshot = replay_event_records(projected_events["events"])
    else:
        if not projection_metadata:
            projection_metadata = _projection_check_metadata(work_root)
        snapshot = replay_events(events_path) if events_path.exists() else {}
    state = _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json")
    if not state:
        state = _read_json_if_exists(run_dir / "state" / "scheduler_state.json")
    worker_registry = _read_json_if_exists(run_dir / "state" / "worker_process_registry.json")
    if not worker_registry:
        worker_registry = _read_json_if_exists(run_dir / "state" / "worker_registry.json")
    liveness = build_run_liveness_summary(run_dir, profile=profile)
    task_counts = _status_task_counts(snapshot, state)
    integration_counts = _status_integration_counts(snapshot)
    manual_gate_count = _waiting_manual_gate_count(snapshot)
    permission_request_count = _waiting_permission_request_count(snapshot)
    permission_request_details = _waiting_permission_request_details(snapshot, run_dir)
    authoring = _build_project_authoring_summary(profile)
    run_status = _status_run_state(snapshot, state)
    overall_status = _overall_run_status(
        run_status,
        liveness["liveness_status"],
        manual_gate_count,
        permission_request_count,
        authoring,
    )
    run_outcome = _status_run_outcome(run_status, task_counts)
    summary = {
        "project": profile.get("project_key") or "unknown",
        "latest_run": run_dir.name,
        "status": run_status,
        "overall_status": overall_status,
        "run_status": run_status,
        "run_outcome": run_outcome,
        "active_phase": _active_phase_for_status(overall_status),
        "active_authoring": _active_authoring_record(authoring),
        "liveness_status": liveness["liveness_status"],
        "runtime_release": liveness["runtime_release"],
        "processes": liveness["processes"],
        "tasks": task_counts,
        "integration": integration_counts,
        "evidence": _status_evidence_counts(state),
        "integration_baseline": _paths_integration_baseline(run_dir, state),
        "inflight": _status_inflight_attempts(state),
        "inactive_inflight": _status_inactive_inflight_attempts(state),
        "workers": _status_worker_counts(worker_registry),
        "last_worker": _status_last_worker(worker_registry),
        "token_usage": token_usage_from_state(state),
        "manual_gates": manual_gate_count,
        "permission_requests": permission_request_count,
        "permission_request_details": permission_request_details,
        "pursue_recap": find_pursue_recap_for_run(run_dir),
        "last_failure": _status_last_failure(snapshot, state),
        "authoring": authoring,
        "run_dir": str(run_dir),
        **projection_metadata,
    }
    gate_summary = _post_backlog_gate_summary(profile, run_dir)
    if gate_summary is not None:
        summary["post_backlog_gates"] = gate_summary
        operator_view = (
            gate_summary.get("operator_view")
            if isinstance(gate_summary.get("operator_view"), dict)
            else {}
        )
        if isinstance(operator_view.get("integration_baseline"), dict):
            summary["integration_baseline"] = operator_view["integration_baseline"]
        summary["operator_review"] = operator_view
        if not gate_summary.get("all_passed") and not _status_summary_is_active(summary):
            summary.update(
                {
                    "status": "awaiting_post_backlog_gates",
                    "overall_status": "awaiting_post_backlog_gates",
                    "run_status": "awaiting_post_backlog_gates",
                    "run_outcome": "pending",
                    "active_phase": "post_backlog_gates",
                }
            )
    return _with_prioritized_status_guidance(summary)


def _with_prioritized_status_guidance(summary):
    guidance = _status_operator_guidance(summary)
    if not guidance:
        return summary
    summary = dict(summary)
    if summary.get("projection_warning"):
        if summary.get("next_action"):
            summary.setdefault("projection_next_action", summary["next_action"])
        if summary.get("operator_hint"):
            summary.setdefault("projection_operator_hint", summary["operator_hint"])
    summary["next_action"] = guidance["next_action"]
    if guidance.get("operator_hint"):
        summary["operator_hint"] = guidance["operator_hint"]
    return summary


def _status_operator_guidance(summary):
    run_id = summary.get("latest_run") or "latest"
    run_dir = summary.get("run_dir") or "<run>"
    permission_requests = int(summary.get("permission_requests") or 0)
    if permission_requests:
        return {
            "next_action": f"agentteam permissions list --run-dir {run_dir}",
            "operator_hint": "Resolve pending permission requests before continuing runtime work.",
        }
    manual_gates = int(summary.get("manual_gates") or 0)
    if manual_gates:
        return {
            "next_action": f"agentteam resume --run-dir {run_dir} --interactive",
            "operator_hint": "Answer the pending manual gate before continuing runtime work.",
        }
    if _status_summary_is_active(summary):
        return {
            "next_action": f"agentteam watch --taskpack {run_id}; agentteam status --run-dir {run_dir}",
            "operator_hint": "The run is still active; watch progress before reviewing integration results.",
        }
    gate_summary = (
        summary.get("post_backlog_gates")
        if isinstance(summary.get("post_backlog_gates"), dict)
        else None
    )
    if gate_summary is not None and not gate_summary.get("all_passed"):
        return {
            "next_action": gate_summary.get("next_action")
            or f"agentteam report --taskpack {run_id}",
            "operator_hint": (
                "Complete the declared post-backlog gates before integration is exposed."
            ),
        }
    if gate_summary is not None and gate_summary.get("all_passed"):
        operator_view = (
            summary.get("operator_review")
            if isinstance(summary.get("operator_review"), dict)
            else {}
        )
        commands = operator_view.get("review_commands") or {}
        baseline = (
            summary.get("integration_baseline")
            if isinstance(summary.get("integration_baseline"), dict)
            else {}
        )
        if commands.get("integrate") and not _integration_baseline_is_handled(
            baseline
        ):
            actions = [
                commands.get("report") or f"agentteam report --taskpack {run_id}",
                commands.get("paths") or f"agentteam paths --taskpack {run_id}",
            ]
            if commands.get("diff"):
                actions.append(commands["diff"])
            actions.append(
                f"review the validated gate result before {commands['integrate']}"
            )
            return {
                "next_action": "; ".join(actions),
                "operator_hint": (
                    "All post-backlog gates passed; review the fresh Git-bound "
                    "integration head before source merge, push, or release activation."
                ),
            }
    pursue_recap = summary.get("pursue_recap") if isinstance(summary.get("pursue_recap"), dict) else {}
    pursue_action = _first_non_empty_text(pursue_recap.get("operator_next_action"))
    pursue_queue = _effective_pursue_queue_for_status(summary, pursue_recap, pursue_action)
    pursue_queue_has_no_goal = _pursue_queue_has_no_auto_dispatchable_item(pursue_queue)
    if pursue_action and not pursue_queue_has_no_goal:
        return {
            "next_action": pursue_action,
            "operator_hint": "Use the pursue or follow-up queue guidance before projection DB maintenance.",
        }
    baseline = summary.get("integration_baseline") if isinstance(summary.get("integration_baseline"), dict) else {}
    if baseline.get("branch") and not _integration_baseline_is_handled(baseline):
        commands = _review_commands_for_run(run_id, baseline)
        action_parts = [
            commands.get("report") or f"agentteam report --taskpack {run_id}",
            commands.get("paths") or f"agentteam paths --taskpack {run_id}",
        ]
        if baseline.get("worktree_exists") and commands.get("diff"):
            action_parts.append(commands["diff"])
        action_parts.append(
            f"review integration baseline before {commands.get('integrate') or f'agentteam integrate --taskpack {run_id}'}"
        )
        return {
            "next_action": "; ".join(action_parts),
            "operator_hint": (
                "Review the integration baseline before source merge, push, or release activation."
            ),
        }
    tasks = summary.get("tasks") if isinstance(summary.get("tasks"), dict) else {}
    integration = summary.get("integration") if isinstance(summary.get("integration"), dict) else {}
    if int(tasks.get("blocked") or 0) or int(integration.get("blocked") or 0):
        return {
            "next_action": f"agentteam report --taskpack {run_id}",
            "operator_hint": "Review blocked task or integration evidence before continuing.",
        }
    if pursue_action and pursue_queue_has_no_goal:
        return {
            "next_action": f"agentteam report --taskpack {run_id}",
            "operator_hint": (
                "The latest follow-up queue has no auto-dispatchable item; "
                "review the report or provide a new concrete goal."
            ),
        }
    return None


def _integration_baseline_is_handled(baseline):
    status = str((baseline or {}).get("status") or "").strip().lower()
    return status in {"acknowledged", "integrated"}


def _effective_pursue_queue_for_status(summary, pursue_recap, pursue_action):
    latest_queue = pursue_recap.get("latest_follow_up_queue") if isinstance(pursue_recap, dict) else None
    if "agentteam queue next" not in str(pursue_action or ""):
        return latest_queue
    refreshed = _recompute_pursue_queue_for_status(summary, pursue_recap)
    return refreshed if isinstance(refreshed, dict) else latest_queue


def _recompute_pursue_queue_for_status(summary, pursue_recap):
    try:
        run_dir_text = _first_non_empty_text(summary.get("run_dir"))
        if not run_dir_text:
            return None
        run_dir = Path(run_dir_text).resolve()
        work_root = _infer_work_root_from_run_dir(run_dir)
        latest_taskpack_id = (
            _first_non_empty_text(pursue_recap.get("latest_taskpack_id"))
            or _first_non_empty_text(summary.get("latest_run"))
            or run_dir.name
        )
        source_run_dir = run_dir
        if latest_taskpack_id and run_dir.name != latest_taskpack_id:
            source_run_dir = (work_root / "runs" / latest_taskpack_id).resolve()
        source_report = build_run_completion_report(
            source_run_dir,
            project=summary.get("project"),
            write_files=False,
        )
        goal_memory = _latest_goal_memory_for_run(work_root, source_run_dir, source_report)
        queue_summary = _pursue_follow_up_queue_summary(
            source_report=source_report,
            goal_memory=goal_memory,
            source_run_dir=source_run_dir,
        )
    except Exception:
        return None
    return _compact_pursue_queue_summary(queue_summary)


def _pursue_queue_has_no_auto_dispatchable_item(latest_queue):
    if not isinstance(latest_queue, dict):
        return False
    return str(latest_queue.get("queue_status") or "").strip().lower() in {
        "empty",
        "no_auto_dispatchable_items",
    }


def _status_summary_is_active(summary):
    overall_status = str(summary.get("overall_status") or "").strip().lower()
    run_status = str(summary.get("run_status") or summary.get("status") or "").strip().lower()
    liveness_status = str(summary.get("liveness_status") or "").strip().lower()
    if (
        run_status in _INACTIVE_INFLIGHT_RUN_STATUSES
        and liveness_status not in {"running-alive", "running-stale"}
    ):
        return False
    if overall_status in {"authoring", "running"} or run_status == "running":
        return True
    inflight = summary.get("inflight") if isinstance(summary.get("inflight"), dict) else {}
    if int(inflight.get("total") or 0) > 0:
        return True
    workers = summary.get("workers") if isinstance(summary.get("workers"), dict) else {}
    if int(workers.get("running") or 0) > 0:
        return True
    authoring = summary.get("authoring") if isinstance(summary.get("authoring"), dict) else {}
    return int(authoring.get("active_count") or 0) > 0


def _build_paths_summary(args, profile, run_dir):
    work_root = Path(profile["work_root"]).resolve()
    run_dir = Path(run_dir).resolve()
    if getattr(args, "project_root", None):
        project_root = Path(args.project_root).resolve()
    elif not getattr(args, "run_dir", None):
        project_root = Path(".").resolve()
    else:
        project_root = None
    state = _paths_run_state(run_dir)
    final_report = run_dir / "reports" / "final_report.md"
    integration_baseline = _paths_integration_baseline(run_dir, state)
    gate_summary = _post_backlog_gate_summary(profile, run_dir)
    operator_view = None
    if gate_summary is not None:
        operator_view = (
            gate_summary.get("operator_view")
            if isinstance(gate_summary.get("operator_view"), dict)
            else {}
        )
        if isinstance(operator_view.get("integration_baseline"), dict):
            integration_baseline = operator_view["integration_baseline"]
        review_commands = dict(operator_view.get("review_commands") or {})
    else:
        review_commands = _review_commands_for_run(run_dir.name, integration_baseline)
    return {
        "project": profile.get("project_key") or "unknown",
        "project_root": str(project_root) if project_root else None,
        "profile_path": str(profile_path_for_project(project_root).resolve()) if project_root else None,
        "work_root": str(work_root),
        "draft_root": str((work_root / "drafts").resolve()),
        "frozen_root": str((work_root / "frozen").resolve()),
        "run_root": str((work_root / "runs").resolve()),
        "artifacts_root": str((work_root / "artifacts").resolve()),
        "releases_root": str((work_root / "releases").resolve()),
        "latest_run": run_dir.name,
        "run_dir": str(run_dir),
        "worker_worktrees_root": str((run_dir / "worktrees").resolve()),
        "artifact_snapshot_root": str((work_root / "artifacts" / "runs" / run_dir.name).resolve()),
        "final_report": str(final_report.resolve()),
        "final_report_exists": final_report.exists(),
        "integration_baseline": integration_baseline,
        "review_commands": review_commands,
        "post_backlog_gates": gate_summary,
        "operator_review": operator_view,
        "read_only_review_commands": {
            key: review_commands[key]
            for key in ["report", "paths", "diff"]
            if key in review_commands
        },
        "accept_command": review_commands.get("integrate"),
    }


def _paths_run_state(run_dir):
    state = _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json")
    if not state:
        state = _read_json_if_exists(run_dir / "state" / "scheduler_state.json")
    return state


def _paths_integration_baseline(run_dir, state):
    baseline = state.get("integration_baseline") if isinstance(state, dict) else {}
    if not isinstance(baseline, dict):
        baseline = {}
    worktree_path = baseline.get("integration_baseline_worktree_path")
    baseline_worktree = (run_dir / "integration-baseline").resolve()
    if not worktree_path and baseline_worktree.exists():
        worktree_path = str(baseline_worktree)
    branch = baseline.get("integration_baseline_branch")
    if not branch and worktree_path:
        branch = f"agentteam/run/{run_dir.name}/integration"
    return {
        "branch": branch,
        "worktree_path": worktree_path,
        "worktree_exists": Path(worktree_path).exists() if worktree_path else False,
        "status": baseline.get("integration_baseline_status"),
        "handled_at": baseline.get("integration_handled_at"),
        "handled_by": baseline.get("integration_handled_by"),
        "handled_head_sha": baseline.get("integration_handled_head_sha"),
        "base_sha": _paths_integration_base_sha(state),
        "head_sha": baseline.get("integration_baseline_head_sha"),
    }


def _paths_integration_base_sha(state):
    steps = state.get("steps") if isinstance(state, dict) else []
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if isinstance(result, dict) and result.get("integration_base_sha"):
            return result["integration_base_sha"]
    return None


def _write_paths_text(summary):
    baseline = summary.get("integration_baseline") or {}
    review_commands = summary.get("review_commands") or {}
    operator_review = summary.get("operator_review") or {}
    review_gate = operator_review.get("review_gate") or {}
    lines = [
        f"project: {summary['project']}",
        f"project_root: {summary.get('project_root') or 'unknown'}",
        f"work_root: {summary['work_root']}",
        f"latest_run: {summary['latest_run']}",
        f"run_dir: {summary['run_dir']}",
        f"artifacts_root: {summary['artifacts_root']}",
        f"final_report: {summary['final_report']}",
        f"integration_baseline_branch: {baseline.get('branch') or 'none'}",
        f"integration_baseline_worktree: {baseline.get('worktree_path') or 'none'}",
        f"integration_baseline_base: {baseline.get('base_sha') or 'unknown'}",
        f"integration_baseline_head: {baseline.get('head_sha') or 'unknown'}",
        f"integration_authority: {baseline.get('authority') or 'scheduler_state'}",
    ]
    if operator_review:
        lines.extend(
            [
                f"gate_epoch: {operator_review.get('gate_epoch') or 'none'}",
                f"gate_integration_head: {operator_review.get('integration_head_sha') or 'unknown'}",
            ]
        )
    if review_gate:
        lines.append(
            "P1-06E_relation: "
            f"{review_gate.get('commit_field') or 'commit'} "
            f"{review_gate.get('integration_head_relation') or 'unknown'} "
            "integration_head"
        )
        for path in review_gate.get("changed_paths") or []:
            lines.append(f"P1-06E_changed_path: {path}")
        approval = review_gate.get("validated_approval") or {}
        if approval.get("operator_identity"):
            lines.append(
                f"P1-06E_validated_approval_identity: {approval['operator_identity']}"
            )
    if operator_review.get("repair_action"):
        lines.append(f"repair_action: {operator_review['repair_action']}")
    if review_commands:
        for key in ["report", "paths", "diff", "approve", "integrate"]:
            command = review_commands.get(key)
            if command:
                lines.append(f"review_{key}: {command}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _require_post_backlog_gate_context(profile, run_dir):
    context = _post_backlog_gate_context(profile, run_dir)
    if context is None:
        raise AgentTeamCliError(
            "frozen taskpack does not declare post-backlog gates",
            run_dir=str(Path(run_dir).resolve()),
        )
    if not context["run_dir"].is_dir():
        raise AgentTeamCliError("implementation run directory not found", run_dir=str(context["run_dir"]))
    return context


def _require_gate_declaration(context, gate_id):
    declaration = context["declarations_by_id"].get(gate_id)
    if declaration is None:
        raise AgentTeamCliError(
            "gate is not declared by the frozen taskpack",
            gate_id=gate_id,
            declared_gates=sorted(context["declarations_by_id"]),
        )
    return declaration


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value):
    return _sha256_bytes(_canonical_json_bytes(value))


def _atomic_write_json(path, value, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise AgentTeamCliError(
                    "immutable gate artifact already exists",
                    path=str(path),
                ) from exc
            temporary.unlink()
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _gate_mutation_locks(context, gate_ids):
    context["locks_root"].mkdir(parents=True, exist_ok=True)
    acquired = []
    lock_names = ["gate-state"] + [f"epoch-gate-{gate_id}" for gate_id in sorted(set(gate_ids))]
    try:
        for name in lock_names:
            path = context["locks_root"] / f"{name}.lock"
            stream = path.open("a+")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                stream.close()
                raise AgentTeamCliError(
                    "post-backlog gate mutation is active",
                    active_lock=name,
                    lock_path=str(path),
                ) from exc
            acquired.append((name, stream))
        yield
    finally:
        for _name, stream in reversed(acquired):
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


def _gate_schema_path(schema_name):
    return Path(__file__).resolve().parents[2] / "schemas" / schema_name


def _validate_gate_record_schema(schema_name, value):
    schema_path = _gate_schema_path(schema_name)
    if not schema_path.is_file():
        raise AgentTeamCliError("bundled gate schema is missing", schema_path=str(schema_path))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    _validate_json_schema(schema, value, source=str(schema_path))


def _validate_json_schema(schema, value, *, source):
    try:
        import jsonschema

        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        errors = sorted(
            validator_class(schema, format_checker=jsonschema.FormatChecker()).iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except Exception as exc:
        if exc.__class__.__module__.startswith("jsonschema"):
            raise AgentTeamCliError("gate schema is invalid", schema_source=source, detail=str(exc)) from exc
        raise
    if errors:
        rendered = []
        for error in errors:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise AgentTeamCliError(
            "gate artifact schema validation failed",
            schema_source=source,
            validation_errors=rendered,
        )


def _publish_gate_epoch(context, record):
    epochs_root = context["epochs_root"]
    epochs_root.mkdir(parents=True, exist_ok=True)
    epoch_dir = epochs_root / str(record["epoch_number"])
    staging = context["gate_root"] / f".epoch-{record['epoch_number']}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        (staging / "receipts").mkdir()
        (staging / "approvals").mkdir()
        _atomic_write_json(staging / "epoch.v1.json", record, replace=False)
        _fsync_directory(staging / "receipts")
        _fsync_directory(staging / "approvals")
        _fsync_directory(staging)
        _rename_directory_noreplace(staging, epoch_dir)
        _fsync_directory(epochs_root)
        return epoch_dir
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _rename_directory_noreplace(source, target):
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise AgentTeamCliError("Linux renameat2 is required for atomic gate epoch publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise AgentTeamCliError(
            "conflicting gate epoch directory already exists",
            path=str(target),
        )
    raise AgentTeamCliError(
        "atomic gate epoch publication failed",
        source=str(source),
        target=str(target),
        errno=error_number,
        detail=os.strerror(error_number),
    )


def _read_current_gate_epoch(context):
    root = context["epochs_root"]
    if not root.exists():
        return None
    entries = [entry for entry in root.iterdir()]
    if not entries:
        return None
    invalid_names = sorted(entry.name for entry in entries if not entry.name.isdigit() or int(entry.name) < 1)
    if invalid_names:
        raise AgentTeamCliError(
            "invalid gate epoch directory entry",
            invalid_epoch_entries=invalid_names,
        )
    numbers = sorted(int(entry.name) for entry in entries)
    if numbers != list(range(1, numbers[-1] + 1)):
        raise AgentTeamCliError("gate epoch sequence has a gap", epoch_numbers=numbers)
    prior_digest = None
    current = None
    for number in numbers:
        epoch_dir = root / str(number)
        if not epoch_dir.is_dir():
            raise AgentTeamCliError("gate epoch entry is not a directory", path=str(epoch_dir))
        record_path = epoch_dir / "epoch.v1.json"
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentTeamCliError("gate epoch record is missing or invalid", path=str(record_path)) from exc
        _validate_gate_record_schema("post_backlog_gate_epoch.schema.json", record)
        if record.get("implementation_run_id") != context["run_dir"].name:
            raise AgentTeamCliError("gate epoch binds a different implementation run", path=str(record_path))
        if record.get("epoch_number") != number:
            raise AgentTeamCliError("gate epoch number does not match directory", path=str(record_path))
        if record.get("prior_epoch_sha256") != prior_digest:
            raise AgentTeamCliError("gate epoch digest chain is invalid", path=str(record_path))
        if record.get("gate_declaration_sha256") != _sha256_json(context["declarations"]):
            raise AgentTeamCliError("gate epoch declaration digest is stale", path=str(record_path))
        digest = _sha256_json(record)
        prior_digest = digest
        current = {"record": record, "digest": digest, "path": epoch_dir}
    return current


def _require_current_gate_epoch(context, expected_number):
    current = _read_current_gate_epoch(context)
    if current is None:
        raise AgentTeamCliError("gate baseline has not been sealed")
    if current["record"]["epoch_number"] != expected_number:
        raise AgentTeamCliError(
            "gate epoch is stale",
            expected_gate_epoch=expected_number,
            current_gate_epoch=current["record"]["epoch_number"],
        )
    return current


def _git_object_format(project_root):
    value = _git_stdout(project_root, ["rev-parse", "--show-object-format"])
    if value not in {"sha1", "sha256"}:
        raise AgentTeamCliError("unsupported repository Git object format", git_object_format=value)
    return value


def _valid_git_oid(value, object_format):
    expected_length = 40 if object_format == "sha1" else 64
    return (
        isinstance(value, str)
        and len(value) == expected_length
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_expected_git_oid(project_root, expected, actual, *, field_name):
    object_format = _git_object_format(project_root)
    if not _valid_git_oid(expected, object_format):
        raise AgentTeamCliError(
            f"{field_name} is not a canonical {object_format} Git OID",
            value=expected,
            git_object_format=object_format,
        )
    if expected != actual:
        raise AgentTeamCliError(
            f"{field_name} changed",
            expected=expected,
            actual=actual,
        )


def _resolved_epoch_integration_head(project_root, epoch):
    if _git_object_format(project_root) != epoch["git_object_format"]:
        raise AgentTeamCliError("repository Git object format does not match gate epoch")
    head = _git_stdout(
        project_root,
        ["rev-parse", "--verify", f"{epoch['integration_branch']}^{{commit}}"],
    )
    if not _valid_git_oid(head, epoch["git_object_format"]):
        raise AgentTeamCliError("resolved integration head has invalid Git object format")
    return head


def _require_path_within(path, root, label):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise AgentTeamCliError(f"{label} escapes its authority root", path=str(path), root=str(root)) from exc


def _gate_receipt_path(context, epoch, gate_id):
    return context["epochs_root"] / str(epoch["epoch_number"]) / "receipts" / f"{gate_id}.receipt.v1.json"


def _gate_approval_path(context, epoch, gate_id):
    return context["epochs_root"] / str(epoch["epoch_number"]) / "approvals" / f"{gate_id}.approval.v1.json"


def _open_gate_controller_invocations(context, gate_id=None):
    findings = []
    runs_root = context["work_root"] / "runs"
    if not runs_root.is_dir():
        return findings
    for path in runs_root.glob("*/state/**/*"):
        if not path.is_file() or not path.name.endswith(".json"):
            continue
        lowered = path.name.lower()
        if "invocation" not in lowered and "claim" not in lowered:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = value if isinstance(value, list) else [value]
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("implementation_run_id") not in {None, context["run_dir"].name}:
                continue
            if gate_id is not None and record.get("gate_id") not in {None, gate_id}:
                continue
            status = str(
                record.get("status")
                or record.get("invocation_status")
                or record.get("lifecycle_status")
                or ""
            ).lower()
            terminal = status in {"completed", "failed", "cancelled", "timed_out", "recovered", "terminal"}
            looks_open = status in {"open", "running", "starting", "in_progress", "claimed"} or (
                record.get("invocation_id") and not terminal and not record.get("ended_at")
            )
            if looks_open:
                findings.append(
                    {
                        "path": str(path),
                        "invocation_id": record.get("invocation_id"),
                        "gate_id": record.get("gate_id"),
                        "status": status or "open",
                    }
                )
    return findings[:20]


def _require_clean_gate_schema_paths(project_root, declaration):
    paths = [declaration["evidence_schema"]]
    if declaration.get("operator_approval_schema"):
        paths.append(declaration["operator_approval_schema"])
    completed = _git_completed(
        project_root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--", *paths],
        check=False,
    )
    if completed.returncode != 0 or completed.stdout.strip():
        raise AgentTeamCliError(
            "gate schema paths must be clean in the integration worktree",
            schema_paths=paths,
            dirty_status=completed.stdout.strip(),
        )


def _git_worktree_for_branch(project_root, branch, expected_head):
    full_ref = _git_stdout(
        project_root,
        ["rev-parse", "--symbolic-full-name", branch],
    )
    listing = _git_stdout(project_root, ["worktree", "list", "--porcelain"])
    matches = []
    for block in listing.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                fields[key] = value
            elif line:
                fields[line] = True
        if fields.get("branch") == full_ref:
            matches.append(fields)
    if len(matches) != 1:
        raise AgentTeamCliError(
            "current gate integration branch does not have exactly one attached worktree",
            integration_branch=branch,
            matching_worktree_count=len(matches),
        )
    record = matches[0]
    worktree_text = record.get("worktree")
    if not worktree_text or not Path(worktree_text).is_dir():
        raise AgentTeamCliError(
            "current gate integration worktree is missing",
            integration_branch=branch,
            worktree_path=worktree_text,
        )
    worktree = Path(worktree_text).resolve()
    attached = _git_completed(
        worktree,
        ["symbolic-ref", "--quiet", "HEAD"],
        check=False,
    )
    if attached.returncode != 0 or attached.stdout.strip() != full_ref:
        raise AgentTeamCliError(
            "current gate integration worktree is detached or attached to another branch",
            integration_branch=branch,
            worktree_path=str(worktree),
            attached_ref=attached.stdout.strip() or None,
        )
    worktree_head = _git_stdout(worktree, ["rev-parse", "HEAD"])
    listed_head = record.get("HEAD")
    if worktree_head != expected_head or listed_head != expected_head:
        raise AgentTeamCliError(
            "current gate integration branch ref and worktree head do not match",
            integration_branch=branch,
            branch_head=expected_head,
            worktree_head=worktree_head,
            listed_worktree_head=listed_head,
        )
    return worktree


def _gate_integration_worktree(context, epoch=None):
    if isinstance(epoch, dict) and epoch.get("integration_branch"):
        head = _resolved_epoch_integration_head(context["project_root"], epoch)
        return _git_worktree_for_branch(
            context["project_root"],
            epoch["integration_branch"],
            head,
        )
    baseline = _paths_integration_baseline(
        context["run_dir"],
        _paths_run_state(context["run_dir"]),
    )
    branch = baseline.get("branch")
    if not branch:
        raise AgentTeamCliError("integration baseline branch is unavailable")
    head = _git_stdout(
        context["project_root"],
        ["rev-parse", "--verify", f"{branch}^{{commit}}"],
    )
    return _git_worktree_for_branch(context["project_root"], branch, head)


def _schema_from_git(project_root, head, schema_path):
    completed = _git_completed(
        project_root,
        ["show", f"{head}:{schema_path}"],
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamCliError(
            "committed gate schema is missing at the current integration head",
            integration_head=head,
            schema_path=schema_path,
        )
    try:
        schema = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AgentTeamCliError(
            "committed gate schema is not valid JSON",
            integration_head=head,
            schema_path=schema_path,
        ) from exc
    return schema, _sha256_bytes(completed.stdout.encode("utf-8"))


def _validate_schema_from_git(project_root, head, schema_path, value):
    schema, digest = _schema_from_git(project_root, head, schema_path)
    _validate_json_schema(schema, value, source=f"{head}:{schema_path}")
    return digest


def _gate_review_diff_sha256(project_root, epoch, head, *, expected_paths=None):
    parent = _git_stdout(project_root, ["rev-parse", f"{head}^"])
    if expected_paths is not None:
        changed = _git_stdout(
            project_root,
            ["diff", "--name-only", f"{parent}..{head}"],
        ).splitlines()
        if sorted(changed) != sorted(expected_paths):
            raise AgentTeamCliError(
                "final report commit does not contain the exact two-path review diff",
                expected_paths=expected_paths,
                changed_paths=changed,
            )
    completed = _git_completed(
        project_root,
        ["diff", "--binary", f"{parent}..{head}", "--", *(expected_paths or [])],
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamCliError("unable to compute gate review diff")
    return _sha256_bytes(completed.stdout.encode("utf-8"))


def _require_operator_approval_context():
    context_values = " ".join(
        os.environ.get(name, "")
        for name in (
            "AGENTTEAM_AGENT_ROLE",
            "AGENTTEAM_EXECUTION_CONTEXT",
            "AGENTTEAM_RUNTIME_ROLE",
            "AGENTTEAM_ACTOR_ROLE",
            "AGENTTEAM_WORKER_ID",
            "AGENT_ROLE",
        )
    ).lower()
    if any(marker in context_values for marker in ("worker", "controller", "scheduler", "agent-")):
        raise AgentTeamCliError("operator approval is forbidden in worker/controller execution context")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AgentTeamCliError("operator approval requires an interactive TTY")


def _evaluate_post_backlog_gates(context, *, current=None):
    if current is None:
        current = _read_current_gate_epoch(context)
    if current is None:
        return {
            "state": "awaiting_validated_baseline",
            "epoch_number": None,
            "epoch_sha256": None,
            "all_passed": False,
            "gates": [
                {
                    "gate_id": declaration["gate_id"],
                    "status": "pending",
                    "state": "pending",
                    "reasons": ["validated baseline epoch is missing"],
                }
                for declaration in context["declarations"]
            ],
        }
    captured_digest = current["digest"]
    decisions = {}
    ordered = []
    unresolved = list(context["declarations"])
    while unresolved:
        progressed = False
        for declaration in list(unresolved):
            gate_dependencies = [
                dependency
                for dependency in declaration.get("depends_on", [])
                if dependency in context["declarations_by_id"]
            ]
            if any(dependency not in decisions for dependency in gate_dependencies):
                continue
            decision = _evaluate_one_post_backlog_gate(
                context,
                current,
                declaration,
                prior_decisions=decisions,
            )
            decisions[declaration["gate_id"]] = decision
            ordered.append(decision)
            unresolved.remove(declaration)
            progressed = True
        if not progressed:
            for declaration in unresolved:
                decision = {
                    "gate_id": declaration["gate_id"],
                    "status": "failed",
                    "state": "failed",
                    "reasons": ["gate dependency graph cannot be evaluated"],
                }
                decisions[declaration["gate_id"]] = decision
                ordered.append(decision)
            break
    reread = _read_current_gate_epoch(context)
    if reread is None or reread["digest"] != captured_digest:
        raise AgentTeamCliError("gate epoch changed during read-only evaluation")
    active_controllers = _open_gate_controller_invocations(context)
    if active_controllers:
        active_gate_ids = {
            item.get("gate_id") for item in active_controllers if item.get("gate_id")
        }
        for item in ordered:
            if not active_gate_ids or item["gate_id"] in active_gate_ids:
                item.setdefault("reasons", []).append(
                    "controller invocation is still open"
                )
                if item.get("state") == "passed":
                    item["state"] = "failed"
                    item["status"] = "failed"
    all_passed = (
        not active_controllers
        and bool(ordered)
        and all(item["state"] == "passed" for item in ordered)
    )
    return {
        "state": "passed" if all_passed else "pending",
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "all_passed": all_passed,
        "active_controllers": active_controllers,
        "gates": ordered,
    }


def _evaluate_one_post_backlog_gate(context, current, declaration, *, prior_decisions):
    gate_id = declaration["gate_id"]
    reasons = []
    dependency_states = {
        dependency: prior_decisions[dependency]["state"]
        for dependency in declaration.get("depends_on", [])
        if dependency in prior_decisions
    }
    if any(state != "passed" for state in dependency_states.values()):
        reasons.append("declared gate dependency is not passed")
    receipt_path = _gate_receipt_path(context, current["record"], gate_id)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_gate_record_schema("post_backlog_gate_receipt.schema.json", receipt)
    except Exception as exc:
        if isinstance(exc, AgentTeamCliError):
            detail = str(exc)
        else:
            detail = "receipt is missing or invalid"
        return {
            "gate_id": gate_id,
            "status": "failed" if receipt_path.exists() else "pending",
            "state": "failed" if receipt_path.exists() else "pending",
            "reasons": reasons + [detail],
            "receipt_path": str(receipt_path),
        }
    expected_receipt = {
        "implementation_run_id": context["run_dir"].name,
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "gate_id": gate_id,
        "git_object_format": current["record"]["git_object_format"],
        "evidence_artifact": declaration["evidence_artifact"],
        "evidence_schema": declaration["evidence_schema"],
    }
    for key, expected in expected_receipt.items():
        if receipt.get(key) != expected:
            reasons.append(f"receipt {key} binding does not match")
    try:
        head = _resolved_epoch_integration_head(context["project_root"], current["record"])
        if (
            receipt.get("expected_integration_head_sha")
            != current["record"]["integration_head_sha"]
        ):
            reasons.append("receipt integration baseline binding is stale")
        _require_clean_gate_schema_paths(
            _gate_integration_worktree(context, current["record"]),
            declaration,
        )
        relative_run = Path(receipt["evidence_run_relative_path"])
        if relative_run.is_absolute() or ".." in relative_run.parts:
            raise AgentTeamCliError("receipt evidence run path is unsafe")
        evidence_run = (context["work_root"] / relative_run).resolve()
        _require_path_within(evidence_run, context["work_root"] / "runs", "evidence run")
        if evidence_run.name != receipt["evidence_run_id"]:
            raise AgentTeamCliError("receipt evidence run identity does not match path")
        artifact_path = (evidence_run / declaration["evidence_artifact"]).resolve()
        _require_path_within(artifact_path, evidence_run, "evidence artifact")
        artifact_bytes = artifact_path.read_bytes()
        artifact = json.loads(artifact_bytes)
        schema_sha256 = _validate_schema_from_git(
            context["project_root"],
            head,
            declaration["evidence_schema"],
            artifact,
        )
        evidence_sha256 = _sha256_bytes(artifact_bytes)
        if artifact.get(declaration["required_status_field"]) != declaration["required_status_value"]:
            reasons.append("controller artifact required status does not match")
        commit_field = declaration.get("commit_field")
        if commit_field:
            commit_sha = artifact.get(commit_field)
            if not _valid_git_oid(commit_sha, current["record"]["git_object_format"]):
                reasons.append(f"artifact {commit_field} is not a canonical Git OID")
            elif declaration.get("integration_head_relation") == "ancestor_of":
                relation = _git_completed(
                    context["project_root"],
                    ["merge-base", "--is-ancestor", commit_sha, head],
                    check=False,
                )
                if relation.returncode != 0:
                    reasons.append(f"artifact {commit_field} is not an ancestor of integration head")
            elif declaration.get("integration_head_relation") == "equals" and commit_sha != head:
                reasons.append(f"artifact {commit_field} does not equal integration head")
        if gate_id == "P1-06E":
            if artifact.get("validated_code_sha") != current["record"]["validated_code_sha"]:
                reasons.append("finalization artifact validated_code_sha is stale")
            parents = _git_stdout(
                context["project_root"],
                ["rev-list", "--parents", "-n", "1", head],
            ).split()
            if len(parents) != 2:
                reasons.append("final report commit must have exactly one parent")
            elif parents[1] != current["record"]["validated_code_sha"]:
                reasons.append("final report parent does not equal validated_code_sha")
            changed_paths = _git_stdout(
                context["project_root"],
                ["diff", "--name-only", f"{parents[1]}..{head}"],
            ).splitlines() if len(parents) == 2 else []
            if sorted(changed_paths) != sorted(_PHASE1_REPORT_REVIEW_PATHS):
                reasons.append("final report commit does not contain the exact two-path diff")
            if (
                isinstance(artifact.get("changed_paths"), list)
                and sorted(artifact["changed_paths"]) != sorted(_PHASE1_REPORT_REVIEW_PATHS)
            ):
                reasons.append("finalization artifact changed_paths does not match review diff")
    except (AgentTeamCliError, OSError, KeyError, json.JSONDecodeError) as exc:
        reasons.append(str(exc) or exc.__class__.__name__)
        return {
            "gate_id": gate_id,
            "status": "failed",
            "state": "failed",
            "reasons": reasons,
            "receipt_path": str(receipt_path),
        }
    state = "failed" if reasons else "passed"
    approval_path = None
    approval_schema_sha256 = None
    if not reasons and declaration.get("operator_review_required"):
        state = "awaiting_operator_review"
        approval_path = _gate_approval_path(context, current["record"], gate_id)
        try:
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval_schema_sha256 = _validate_schema_from_git(
                context["project_root"],
                head,
                declaration["operator_approval_schema"],
                approval,
            )
            expected_approval = {
                "implementation_run_id": context["run_dir"].name,
                "epoch_number": current["record"]["epoch_number"],
                "epoch_sha256": current["digest"],
                "gate_id": gate_id,
                "decision": declaration["operator_approval_required_decision"],
                "evidence_sha256": evidence_sha256,
                "final_report_sha": head,
                "git_object_format": current["record"]["git_object_format"],
                "review_diff_sha256": _gate_review_diff_sha256(
                    context["project_root"],
                    current["record"],
                    head,
                    expected_paths=(
                        _PHASE1_REPORT_REVIEW_PATHS
                        if gate_id == "P1-06E"
                        else None
                    ),
                ),
            }
            mismatches = [
                key for key, expected in expected_approval.items() if approval.get(key) != expected
            ]
            if mismatches:
                reasons.append("operator approval binding mismatch: " + ", ".join(mismatches))
            else:
                state = "passed"
        except (AgentTeamCliError, OSError, KeyError, json.JSONDecodeError) as exc:
            approval_schema_sha256 = None
            if approval_path.exists():
                reasons.append(str(exc) or "operator approval is invalid")
    return {
        "gate_id": gate_id,
        "status": "passed" if state == "passed" else ("failed" if state == "failed" else "pending"),
        "state": state,
        "reasons": reasons,
        "receipt_path": str(receipt_path),
        "approval_path": str(approval_path) if approval_path else None,
        "evidence_sha256": evidence_sha256,
        "evidence_schema_sha256": schema_sha256,
        "approval_schema_sha256": approval_schema_sha256 if declaration.get("operator_review_required") else None,
        "integration_head_sha": head,
    }


def _complete_gated_milestone_if_ready(context):
    """Freshly evaluate every gate, then emit the canonical completion once."""
    decision = _evaluate_post_backlog_gates(context)
    if not decision.get("all_passed"):
        return None

    taskpack = context["taskpack"]
    files = taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}
    agent_pool_path = (context["frozen_dir"] / files.get("agent_pool", "agent_pool.json")).resolve()
    backlog_path = (context["frozen_dir"] / files.get("backlog", "backlog.json")).resolve()
    notification_sink = _post_backlog_gate_notification_sink(context["profile"])
    scheduler = TwoPhaseFileScheduler(
        agent_pool_path,
        backlog_path,
        context["run_dir"],
        project_root=context["project_root"],
        notification_sink=notification_sink,
    )
    scheduler.state["scheduler_status"] = "completed"
    scheduler._write_state()
    emitted = scheduler._emit_run_event_once(
        "run_completed",
        scheduler._run_event_payload(
            "completed",
            {
                "milestone_status": "completed",
                "post_backlog_gate_epoch": decision.get("epoch_number"),
                "post_backlog_gates": decision.get("gates", []),
            },
        ),
    )
    event_id = (
        emitted[0]["event_id"]
        if emitted
        else scheduler.state.get("run_event_ids", {}).get("run_completed")
    )
    return {
        "run_status": "completed",
        "event_id": event_id,
        "idempotent": not bool(emitted),
        "gate_epoch": decision.get("epoch_number"),
    }


def _post_backlog_gate_notification_sink(profile):
    feishu = profile.get("feishu") if isinstance(profile.get("feishu"), dict) else {}
    if feishu and not feishu.get("enabled", True):
        return None
    webhook_env = feishu.get("webhook_env") if feishu else None
    if not webhook_env:
        return None
    return build_feishu_notification_sink_from_env(
        webhook_env=webhook_env,
        signing_secret_env=feishu.get("signing_secret_env"),
        project=(
            profile.get("notification_project")
            or profile.get("project_key")
            or "agentteam"
        ),
    )


def _gate_operator_repair_action(context, current=None):
    run_id = context["run_dir"].name
    if current is None:
        epochs_root = context["epochs_root"]
        if not epochs_root.exists() or not any(epochs_root.iterdir()):
            return (
                f"agentteam gate seal-baseline --taskpack {run_id} "
                "--expected-integration-head <fresh-integration-head> "
                "--authorize-revalidation"
            )
        epoch_number = "<current-epoch>"
        target_head = "<fresh-target-head>"
    else:
        record = current["record"]
        epoch_number = record["epoch_number"]
        completed = _git_completed(
            context["project_root"],
            ["rev-parse", "--verify", f"{record['target_branch']}^{{commit}}"],
            check=False,
        )
        target_head = completed.stdout.strip() if completed.returncode == 0 else "<fresh-target-head>"
    return (
        f"agentteam gate refresh-baseline --taskpack {run_id} "
        f"--expected-gate-epoch {epoch_number} "
        f"--expected-target-head {target_head} --authorize-revalidation"
    )


def _historical_only_integration_baseline(run_dir):
    cached = _paths_integration_baseline(
        run_dir,
        _paths_run_state(run_dir),
    )
    return {
        "branch": None,
        "worktree_path": None,
        "worktree_exists": False,
        "status": cached.get("status"),
        "handled_at": cached.get("handled_at"),
        "handled_by": cached.get("handled_by"),
        "handled_head_sha": cached.get("handled_head_sha"),
        "base_sha": None,
        "head_sha": None,
        "authority": "failed_closed",
        "historical_scheduler_branch": cached.get("branch"),
        "historical_scheduler_base_sha": cached.get("base_sha"),
        "historical_scheduler_head_sha": cached.get("head_sha"),
    }


def _fresh_gate_operator_view(context, current, decision):
    cached = _paths_integration_baseline(
        context["run_dir"],
        _paths_run_state(context["run_dir"]),
    )
    if current is None:
        branch = cached.get("branch")
        if not branch:
            raise AgentTeamCliError("integration baseline branch is unavailable")
        head = _git_stdout(
            context["project_root"],
            ["rev-parse", "--verify", f"{branch}^{{commit}}"],
        )
        worktree = _git_worktree_for_branch(context["project_root"], branch, head)
        epoch_number = None
        epoch_sha256 = None
        validated_code_sha = cached.get("base_sha") or cached.get("head_sha")
        recorded_epoch_head = None
    else:
        record = current["record"]
        branch = record["integration_branch"]
        head = _resolved_epoch_integration_head(context["project_root"], record)
        worktree = _git_worktree_for_branch(context["project_root"], branch, head)
        epoch_number = record["epoch_number"]
        epoch_sha256 = current["digest"]
        validated_code_sha = record["validated_code_sha"]
        recorded_epoch_head = record["integration_head_sha"]
        relation = _git_completed(
            context["project_root"],
            ["merge-base", "--is-ancestor", validated_code_sha, head],
            check=False,
        )
        if relation.returncode != 0:
            raise AgentTeamCliError(
                "current integration branch no longer descends from its recorded gate baseline",
                integration_branch=branch,
                recorded_baseline_head=validated_code_sha,
                current_branch_head=head,
            )
    reread_head = _git_stdout(
        context["project_root"],
        ["rev-parse", "--verify", f"{branch}^{{commit}}"],
    )
    if reread_head != head:
        raise AgentTeamCliError(
            "current integration branch changed during operator view resolution",
            integration_branch=branch,
            before_head=head,
            after_head=reread_head,
        )
    if current is not None:
        reread_epoch = _read_current_gate_epoch(context)
        if reread_epoch is None or reread_epoch["digest"] != current["digest"]:
            raise AgentTeamCliError("gate epoch changed during operator view resolution")
    for gate in decision.get("gates") or []:
        decision_head = gate.get("integration_head_sha")
        if decision_head and decision_head != head:
            raise AgentTeamCliError(
                "gate decision and operator view resolved different integration heads",
                gate_id=gate.get("gate_id"),
                gate_head=decision_head,
                operator_view_head=head,
            )

    base_sha = validated_code_sha or cached.get("base_sha")
    changed_paths = []
    if base_sha and base_sha != head:
        changed_paths = _git_stdout(
            context["project_root"],
            ["diff", "--name-only", f"{base_sha}..{head}"],
        ).splitlines()
    declarations = context["declarations_by_id"]
    review_declaration = declarations.get("P1-06E")
    review_decision = next(
        (
            gate
            for gate in decision.get("gates") or []
            if gate.get("gate_id") == "P1-06E"
        ),
        None,
    )
    approval_command = None
    validated_approval = None
    if review_decision and review_decision.get("state") == "awaiting_operator_review":
        approval_command = _post_backlog_gate_next_action(context, decision)
    elif (
        current is not None
        and review_decision
        and review_decision.get("state") == "passed"
    ):
        approval = _read_json_if_exists(
            _gate_approval_path(context, current["record"], "P1-06E")
        )
        if approval:
            validated_approval = {
                "operator_identity": approval.get("operator_identity"),
                "decision": approval.get("decision"),
                "reviewed_at": approval.get("reviewed_at"),
                "evidence_sha256": approval.get("evidence_sha256"),
                "review_diff_sha256": approval.get("review_diff_sha256"),
                "final_report_sha": approval.get("final_report_sha"),
            }

    baseline = {
        "branch": branch,
        "worktree_path": str(worktree),
        "worktree_exists": True,
        "status": cached.get("status"),
        "handled_at": cached.get("handled_at"),
        "handled_by": cached.get("handled_by"),
        "handled_head_sha": cached.get("handled_head_sha"),
        "base_sha": base_sha,
        "head_sha": head,
        "authority": "current_gate_epoch_git_ref",
        "gate_epoch": epoch_number,
        "gate_epoch_sha256": epoch_sha256,
        "recorded_epoch_head_sha": recorded_epoch_head,
        "historical_scheduler_branch": cached.get("branch"),
        "historical_scheduler_base_sha": cached.get("base_sha"),
        "historical_scheduler_head_sha": cached.get("head_sha"),
    }
    commands = _review_commands_for_run(context["run_dir"].name, baseline)
    if not decision.get("all_passed"):
        commands.pop("integrate", None)
    if approval_command:
        commands["approve"] = approval_command
    review_gate = None
    if review_declaration:
        review_gate = {
            "gate_id": "P1-06E",
            "state": review_decision.get("state") if review_decision else "pending",
            "integration_head_relation": review_declaration.get(
                "integration_head_relation"
            ),
            "commit_field": review_declaration.get("commit_field"),
            "validated_code_sha": validated_code_sha,
            "final_report_sha": head if changed_paths else None,
            "expected_report_paths": list(_PHASE1_REPORT_REVIEW_PATHS),
            "changed_paths": changed_paths,
            "approval_command": approval_command,
            "validated_approval": validated_approval,
        }
    return {
        "authority": "current_gate_epoch_git_ref",
        "gate_epoch": epoch_number,
        "gate_epoch_sha256": epoch_sha256,
        "integration_branch": branch,
        "integration_head_sha": head,
        "validated_code_sha": validated_code_sha,
        "integration_worktree": str(worktree),
        "historical_scheduler_branch": cached.get("branch"),
        "historical_scheduler_head_sha": cached.get("head_sha"),
        "changed_paths": changed_paths,
        "review_gate": review_gate,
        "review_commands": commands,
        "approval_command": approval_command,
        "validated_approval": validated_approval,
        "integration_baseline": baseline,
        "repair_action": None,
    }


def _post_backlog_gate_summary(profile, run_dir):
    context = _post_backlog_gate_context(profile, run_dir)
    if context is None:
        return None
    current = None
    try:
        current = _read_current_gate_epoch(context)
        decision = _evaluate_post_backlog_gates(context, current=current)
        decision["operator_view"] = _fresh_gate_operator_view(
            context,
            current,
            decision,
        )
    except AgentTeamCliError as exc:
        repair_action = _gate_operator_repair_action(context, current)
        return {
            "state": "failed_closed",
            "all_passed": False,
            "epoch_number": (
                current["record"]["epoch_number"] if current is not None else None
            ),
            "gates": [],
            "error": str(exc),
            "repair_action": repair_action,
            "next_action": repair_action,
            "operator_view": {
                "authority": "failed_closed",
                "gate_epoch": (
                    current["record"]["epoch_number"]
                    if current is not None
                    else None
                ),
                "integration_branch": None,
                "integration_head_sha": None,
                "integration_worktree": None,
                "review_commands": {},
                "repair_action": repair_action,
                "integration_baseline": _historical_only_integration_baseline(
                    context["run_dir"]
                ),
            },
        }
    decision["active_controllers"] = _open_gate_controller_invocations(context)
    decision["next_action"] = _post_backlog_gate_next_action(
        context,
        decision,
        operator_view=decision["operator_view"],
    )
    if decision["operator_view"].get("approval_command"):
        decision["next_action"] = decision["operator_view"]["approval_command"]
    return decision


def _post_backlog_gate_next_action(context, decision, operator_view=None):
    run_id = context["run_dir"].name
    operator_view = operator_view if isinstance(operator_view, dict) else {}
    if decision.get("active_controllers"):
        return f"agentteam status --run-dir {context['run_dir']}"
    if decision.get("epoch_number") is None:
        head = operator_view.get("integration_head_sha")
        if not head:
            try:
                baseline = _paths_integration_baseline(
                    context["run_dir"],
                    _paths_run_state(context["run_dir"]),
                )
                head = _git_stdout(
                    context["project_root"],
                    ["rev-parse", "--verify", f"{baseline['branch']}^{{commit}}"],
                )
            except Exception:
                head = "<integration-head>"
        return (
            f"agentteam gate seal-baseline --taskpack {run_id} "
            f"--expected-integration-head {head} --authorize-revalidation"
        )
    for gate in decision.get("gates", []):
        if gate.get("state") == "passed":
            continue
        gate_id = gate["gate_id"]
        epoch = decision["epoch_number"]
        head = (
            gate.get("integration_head_sha")
            or operator_view.get("integration_head_sha")
            or "<integration-head>"
        )
        if gate.get("state") == "awaiting_operator_review":
            evidence = gate.get("evidence_sha256") or "<evidence-sha256>"
            return (
                f"agentteam gate approve --taskpack {run_id} --gate {gate_id} "
                f"--gate-epoch {epoch} --expected-evidence-sha256 {evidence} "
                f"--expected-integration-head {head} --approve"
            )
        return (
            f"agentteam gate register --taskpack {run_id} --gate {gate_id} "
            f"--gate-epoch {epoch} --evidence-run <evidence-run-id> "
            f"--expected-integration-head {head}"
        )
    return None


def _review_commands_for_run(run_id, baseline):
    if not isinstance(baseline, dict) or not baseline.get("branch"):
        return {}
    worktree_path = baseline.get("worktree_path")
    head_sha = baseline.get("head_sha")
    base_sha = baseline.get("base_sha")
    commands = {
        "report": f"agentteam report --taskpack {run_id}",
        "paths": f"agentteam paths --taskpack {run_id}",
        "integrate": f"agentteam integrate --taskpack {run_id}",
    }
    if worktree_path and base_sha and head_sha:
        commands["diff"] = f"git -C {worktree_path} diff --stat {base_sha}..{head_sha}"
    elif worktree_path and head_sha:
        commands["diff"] = f"git -C {worktree_path} diff --stat {head_sha}..HEAD"
    elif worktree_path:
        commands["diff"] = f"git -C {worktree_path} status --short"
    return commands


def _integrate_run_baseline(project_root, profile, run_dir, rebase=False, record_only=False):
    context = _post_backlog_gate_context(profile, run_dir)
    if context is None:
        return _integrate_run_baseline_unchecked(
            project_root,
            profile,
            run_dir,
            rebase=rebase,
            record_only=record_only,
        )
    if Path(project_root).resolve() != context["project_root"]:
        raise AgentTeamCliError(
            "selected project root does not match frozen gate authority",
            project_root=str(Path(project_root).resolve()),
            frozen_project_root=str(context["project_root"]),
        )
    if rebase:
        raise AgentTeamCliError(
            "rebase is forbidden for a commit-bound post-backlog gate",
            declared_gates=sorted(context["declarations_by_id"]),
        )
    gate_ids = sorted(context["declarations_by_id"])
    with _gate_mutation_locks(context, gate_ids):
        current = _read_current_gate_epoch(context)
        decision = _evaluate_post_backlog_gates(context, current=current)
        try:
            operator_view = _fresh_gate_operator_view(
                context,
                current,
                decision,
            )
        except AgentTeamCliError as exc:
            raise AgentTeamCliError(
                "fresh gate integration view is unavailable",
                reason=str(exc),
                repair_action=_gate_operator_repair_action(context, current),
            ) from exc
        if not decision.get("all_passed"):
            raise AgentTeamCliError(
                "required post-backlog gates are not passed",
                gate_epoch=decision.get("epoch_number"),
                post_backlog_gates=decision.get("gates"),
                next_action=_post_backlog_gate_next_action(
                    context,
                    decision,
                    operator_view=operator_view,
                ),
            )
        return _integrate_run_baseline_unchecked(
            project_root,
            profile,
            run_dir,
            rebase=False,
            record_only=record_only,
            resolved_baseline=operator_view["integration_baseline"],
        )


def _integrate_run_baseline_unchecked(
    project_root,
    profile,
    run_dir,
    rebase=False,
    record_only=False,
    resolved_baseline=None,
):
    run_dir = Path(run_dir).resolve()
    run_status = _build_run_status_summary(profile, run_dir)
    if run_status.get("status") not in {"idle", "completed"}:
        raise AgentTeamCliError(
            "run is not ready to integrate",
            run_dir=str(run_dir),
            run_status=run_status.get("status") or "unknown",
        )
    state = _paths_run_state(run_dir)
    baseline = (
        dict(resolved_baseline)
        if isinstance(resolved_baseline, dict)
        else _paths_integration_baseline(run_dir, state)
    )
    fresh_baseline = (
        dict(resolved_baseline)
        if isinstance(resolved_baseline, dict)
        else None
    )
    branch = baseline.get("branch")
    if not branch:
        raise AgentTeamCliError("integration baseline branch not found", run_dir=str(run_dir))
    branch_head = _git_stdout(project_root, ["rev-parse", "--verify", f"{branch}^{{commit}}"])
    expected_branch_head = (
        baseline.get("head_sha")
        if isinstance(resolved_baseline, dict)
        else None
    )
    if expected_branch_head and branch_head != expected_branch_head:
        raise AgentTeamCliError(
            "integration branch changed after fresh operator view resolution",
            integration_branch=branch,
            expected_head=expected_branch_head,
            actual_head=branch_head,
        )
    current_head = _git_stdout(project_root, ["rev-parse", "HEAD"])
    if record_only:
        recorded_baseline = _mark_integration_baseline_status(
            run_dir,
            "acknowledged",
            target_head=current_head,
            baseline_head=branch_head,
        )
        baseline = _integration_result_baseline(
            recorded_baseline,
            fresh_baseline,
            branch_head,
        )
        return {
            "integrate_status": "acknowledged",
            "merge_status": "record_only",
            "rebase_status": "not_requested",
            "project": profile.get("project_key") or "unknown",
            "taskpack_id": run_dir.name,
            "project_root": str(project_root),
            "run_dir": str(run_dir),
            "integration_baseline": baseline,
            "before_head": current_head,
            "after_head": current_head,
        }
    dirty_status = _git_stdout(project_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty_status:
        raise AgentTeamCliError(
            "target repository must be clean before integrate",
            project_root=str(project_root),
            dirty_status=dirty_status,
        )
    if branch_head == current_head:
        recorded_baseline = _mark_integration_baseline_status(
            run_dir,
            "integrated",
            target_head=current_head,
            baseline_head=branch_head,
        )
        baseline = _integration_result_baseline(
            recorded_baseline,
            fresh_baseline,
            branch_head,
        )
        return {
            "integrate_status": "up_to_date",
            "merge_status": "up_to_date",
            "rebase_status": "not_needed",
            "project": profile.get("project_key") or "unknown",
            "taskpack_id": run_dir.name,
            "project_root": str(project_root),
            "run_dir": str(run_dir),
            "integration_baseline": baseline,
            "before_head": current_head,
            "after_head": current_head,
        }
    ancestor = _git_completed(project_root, ["merge-base", "--is-ancestor", "HEAD", branch], check=False)
    merge_status = "fast_forward"
    if ancestor.returncode != 0:
        if rebase:
            rebase_result = _rebase_integration_baseline(project_root, run_dir, baseline, current_head)
            if rebase_result["rebase_status"] == "conflict":
                return {
                    "integrate_status": "blocked",
                    "merge_status": "not_merged",
                    "rebase_status": "conflict",
                    "project": profile.get("project_key") or "unknown",
                    "taskpack_id": run_dir.name,
                    "project_root": str(project_root),
                    "run_dir": str(run_dir),
                    "integration_baseline": {**baseline, "head_sha": branch_head},
                    "before_head": current_head,
                    "after_head": current_head,
                    "conflicted_files": rebase_result["conflicted_files"],
                    "rebase_stdout": rebase_result.get("stdout", ""),
                    "rebase_stderr": rebase_result.get("stderr", ""),
                    "rebase_abort_status": rebase_result.get("abort_status"),
                }
            branch_head = rebase_result["head_sha"]
            baseline = {**baseline, "head_sha": branch_head}
            merge_status = "rebased_fast_forward"
        else:
            raise AgentTeamCliError(
                "integration baseline is not a fast-forward of target HEAD",
                project_root=str(project_root),
                run_dir=str(run_dir),
                branch=branch,
                current_head=current_head,
                integration_baseline_head=branch_head,
            )
    merge = _git_completed(project_root, ["merge", "--ff-only", branch])
    after_head = _git_stdout(project_root, ["rev-parse", "HEAD"])
    recorded_baseline = _mark_integration_baseline_status(
        run_dir,
        "integrated",
        target_head=after_head,
        baseline_head=branch_head,
    )
    baseline = _integration_result_baseline(
        recorded_baseline,
        fresh_baseline,
        branch_head,
    )
    return {
        "integrate_status": "merged",
        "merge_status": merge_status,
        "rebase_status": "rebased" if merge_status == "rebased_fast_forward" else "not_needed",
        "project": profile.get("project_key") or "unknown",
        "taskpack_id": run_dir.name,
        "project_root": str(project_root),
        "run_dir": str(run_dir),
        "integration_baseline": baseline,
        "before_head": current_head,
        "after_head": after_head,
        "merge_stdout": merge.stdout,
        "merge_stderr": merge.stderr,
    }


def _integration_result_baseline(recorded, fresh, head_sha):
    if not isinstance(fresh, dict):
        return {**recorded, "head_sha": head_sha}
    result = {**recorded, **fresh, "head_sha": head_sha}
    for key in ("status", "handled_at", "handled_by", "handled_head_sha"):
        if recorded.get(key) is not None:
            result[key] = recorded[key]
    return result


def _rebase_integration_baseline(project_root, run_dir, baseline, current_head):
    worktree_path = baseline.get("worktree_path")
    if not worktree_path:
        raise AgentTeamCliError("integration baseline worktree not found", run_dir=str(run_dir))
    worktree = Path(worktree_path)
    if not worktree.exists():
        raise AgentTeamCliError("integration baseline worktree not found", run_dir=str(run_dir), worktree_path=str(worktree))
    dirty_status = _git_stdout(worktree, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty_status:
        raise AgentTeamCliError(
            "integration baseline worktree must be clean before rebase",
            run_dir=str(run_dir),
            worktree_path=str(worktree),
            dirty_status=dirty_status,
        )
    rebase = _git_completed(worktree, ["rebase", current_head], check=False)
    if rebase.returncode != 0:
        conflicted_files = _git_conflicted_files(worktree)
        abort = _git_completed(worktree, ["rebase", "--abort"], check=False)
        return {
            "rebase_status": "conflict",
            "conflicted_files": conflicted_files,
            "stdout": rebase.stdout,
            "stderr": rebase.stderr,
            "abort_status": "aborted" if abort.returncode == 0 else "abort_failed",
            "abort_stdout": abort.stdout,
            "abort_stderr": abort.stderr,
        }
    head_sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    _update_integration_baseline_head(run_dir, head_sha)
    return {
        "rebase_status": "rebased",
        "head_sha": head_sha,
        "stdout": rebase.stdout,
        "stderr": rebase.stderr,
    }


def _git_conflicted_files(repo):
    output = _git_completed(repo, ["diff", "--name-only", "--diff-filter=U"], check=False).stdout
    return [line for line in output.splitlines() if line.strip()]


def _update_integration_baseline_head(run_dir, head_sha):
    state_path = run_dir / "state" / "two_phase_scheduler_state.json"
    if not state_path.exists():
        state_path = run_dir / "state" / "scheduler_state.json"
    if not state_path.exists():
        return
    state = _read_json_if_exists(state_path)
    baseline = state.get("integration_baseline") if isinstance(state.get("integration_baseline"), dict) else {}
    baseline["integration_baseline_head_sha"] = head_sha
    state["integration_baseline"] = baseline
    _write_json(state_path, state)


def _mark_integration_baseline_status(run_dir, status, *, target_head, baseline_head=None):
    state_path = run_dir / "state" / "two_phase_scheduler_state.json"
    if not state_path.exists():
        state_path = run_dir / "state" / "scheduler_state.json"
    if not state_path.exists():
        return {"status": status, "head_sha": baseline_head}
    state = _read_json_if_exists(state_path)
    baseline = state.get("integration_baseline") if isinstance(state.get("integration_baseline"), dict) else {}
    baseline = dict(baseline)
    timestamp = _format_utc_timestamp(datetime.now(UTC))
    baseline["integration_baseline_status"] = status
    baseline["integration_handled_at"] = timestamp
    baseline["integration_handled_by"] = "operator"
    baseline["integration_handled_head_sha"] = target_head
    if baseline_head:
        baseline["integration_handled_baseline_head_sha"] = baseline_head
        baseline.setdefault("integration_baseline_head_sha", baseline_head)
    if status == "acknowledged":
        baseline["integration_acknowledged_at"] = timestamp
        baseline["integration_acknowledged_by"] = "operator"
        baseline["integration_acknowledged_head_sha"] = target_head
    if status == "integrated":
        baseline["integration_integrated_at"] = timestamp
        baseline["integration_integrated_by"] = "operator"
        baseline["integration_integrated_head_sha"] = target_head
    state["integration_baseline"] = baseline
    _write_json(state_path, state)
    return _paths_integration_baseline(run_dir, state)


def _write_integrate_text(summary):
    baseline = summary.get("integration_baseline") or {}
    lines = [
        f"integrate_status: {summary['integrate_status']}",
        f"merge_status: {summary['merge_status']}",
        f"rebase_status: {summary.get('rebase_status') or 'not_requested'}",
        f"project: {summary['project']}",
        f"taskpack_id: {summary['taskpack_id']}",
        f"integration_baseline_branch: {baseline.get('branch') or 'none'}",
        f"before_head: {summary.get('before_head') or 'unknown'}",
        f"after_head: {summary.get('after_head') or 'unknown'}",
        f"run_dir: {summary['run_dir']}",
    ]
    if summary.get("conflicted_files"):
        lines.append(f"conflicted_files: {', '.join(summary['conflicted_files'])}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_notify_text(summary):
    lines = [
        f"notify_status: {summary['notify_status']}",
        f"provider: {summary['provider']}",
        f"project: {summary['project']}",
        f"webhook_env: {summary['webhook_env']}",
        f"signing_enabled: {str(bool(summary.get('signing_enabled'))).lower()}",
    ]
    if summary.get("message_summary"):
        lines.append(f"message_summary: {summary['message_summary']}")
    if summary.get("error_class"):
        lines.append(f"error_class: {summary['error_class']}")
    if summary.get("error_summary"):
        lines.append(f"error_summary: {summary['error_summary']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_notify_diagnose_text(summary):
    lines = [
        f"diagnosis_status: {summary['diagnosis_status']}",
        f"provider: {summary['provider']}",
        f"project: {summary['project']}",
        f"webhook_env: {summary['webhook_env']}",
        f"signing_enabled: {str(bool(summary.get('signing_enabled'))).lower()}",
    ]
    for variant in summary.get("variants") or []:
        attempt_count = variant.get("delivery_attempt_count")
        attempt_text = "" if attempt_count is None else f" attempts={attempt_count}"
        lines.append(
            f"variant {variant.get('variant')}: {variant.get('status')}{attempt_text}"
        )
        if variant.get("error_summary"):
            lines.append(f"  error_summary: {variant['error_summary']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _git_stdout(repo, args):
    return _git_completed(repo, args).stdout.strip()


def _git_completed(repo, args, check=True):
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AgentTeamCliError(
            "git command failed",
            project_root=str(repo),
            git_args=args,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def _write_status_text(summary):
    lines = [
        f"project: {summary['project']}",
        f"latest_run: {summary['latest_run']}",
        f"overall_status: {summary.get('overall_status') or summary['status']}",
        f"run_status: {summary.get('run_status') or summary['status']}",
        f"run_outcome: {summary.get('run_outcome') or summary.get('run_status') or summary['status']}",
        f"liveness: {summary['liveness_status']}",
        *_projection_text_lines(summary),
        (
            "tasks: "
            f"{summary['tasks']['done']} done, "
            f"{summary['tasks']['blocked']} blocked"
        ),
        f"integration: {summary['integration']['blocked']} blocked",
        *_status_evidence_lines(summary.get("evidence")),
        f"integration_baseline_branch: {summary['integration_baseline'].get('branch') or 'none'}",
        f"integration_baseline_status: {summary['integration_baseline'].get('status') or 'unknown'}",
        f"integration_baseline_head: {summary['integration_baseline'].get('head_sha') or 'unknown'}",
        format_token_usage(summary.get("token_usage"), label="tokens"),
        f"inflight: {summary['inflight']['total']}",
        *_inactive_inflight_status_lines(summary.get("inactive_inflight")),
        f"manual_gates: {summary['manual_gates']}",
        f"permission_requests: {summary['permission_requests']}",
        *_pursue_recap_status_lines(
            summary.get("pursue_recap"),
            effective_next_action=summary.get("next_action"),
        ),
    ]
    for request in summary.get("permission_request_details") or []:
        lines.append(
            "permission_request: "
            f"{request.get('request_id') or 'unknown'} "
            f"task={request.get('task_id') or 'unknown'} "
            f"capability={request.get('requested_capability') or 'runtime_permission'}"
        )
        if request.get("reason"):
            lines.append(f"reason: {request['reason']}")
        if request.get("approve_command"):
            lines.append(f"approve: {request['approve_command']}")
        if request.get("deny_command"):
            lines.append(f"deny: {request['deny_command']}")
    if summary.get("active_phase"):
        lines.append(f"active_phase: {summary['active_phase']}")
    active_authoring = summary.get("active_authoring")
    if isinstance(active_authoring, dict):
        lines.append(
            "active_authoring: "
            f"{active_authoring.get('taskpack_id') or 'unknown'} "
            f"liveness={active_authoring.get('liveness_status') or 'unknown'} "
            f"elapsed_seconds={active_authoring.get('elapsed_seconds') or 0}"
        )
    if summary["workers"]["total"]:
        lines.append(
            "workers: "
            f"{summary['workers']['stopped']} stopped, "
            f"{summary['workers']['running']} running, "
            f"{summary['workers']['quarantined']} quarantined"
        )
    if summary.get("last_worker"):
        lines.append(f"last_worker: {summary['last_worker']}")
    if summary.get("last_failure"):
        lines.append(f"last_failure: {summary['last_failure']}")
    authoring = summary.get("authoring") if isinstance(summary.get("authoring"), dict) else {}
    if authoring.get("active_count"):
        latest = authoring.get("latest") or {}
        lines.append(
            "authoring: "
            f"{authoring['active_count']} active "
            f"latest={latest.get('taskpack_id') or 'unknown'} "
            f"liveness={latest.get('liveness_status') or 'unknown'}"
        )
    gate_summary = (
        summary.get("post_backlog_gates")
        if isinstance(summary.get("post_backlog_gates"), dict)
        else None
    )
    if gate_summary is not None:
        lines.append(
            "post_backlog_gates: "
            f"{gate_summary.get('state') or 'unknown'} "
            f"epoch={gate_summary.get('epoch_number') or 'none'}"
        )
        for gate in gate_summary.get("gates") or []:
            lines.append(
                f"post_backlog_gate: {gate.get('gate_id') or 'unknown'} "
                f"status={gate.get('status') or 'unknown'} "
                f"state={gate.get('state') or 'unknown'}"
            )
        if gate_summary.get("active_controllers"):
            lines.append(
                f"post_backlog_gate_active_controllers: "
                f"{len(gate_summary['active_controllers'])}"
            )
        if summary.get("next_action"):
            lines.append(f"next_action: {summary['next_action']}")
    lines.append(f"run_dir: {summary['run_dir']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _pursue_recap_status_lines(recap, *, effective_next_action=None):
    if not isinstance(recap, dict) or not recap:
        return []
    lines = [
        (
            "pursue: "
            f"{recap.get('pursue_id') or 'unknown'} "
            f"stopped because {recap.get('stop_reason') or 'unknown'}"
        ),
        f"pursue_rounds: {recap.get('rounds_completed', 0)}/{recap.get('max_rounds', 0)}",
    ]
    if recap.get("latest_taskpack_id"):
        lines.append(f"pursue_latest_taskpack: {recap['latest_taskpack_id']}")
    if recap.get("latest_report_path"):
        lines.append(f"pursue_latest_report: {recap['latest_report_path']}")
    if recap.get("operator_next_action"):
        action = recap["operator_next_action"]
        if (
            "agentteam queue next" in str(action)
            and effective_next_action
            and str(action) not in str(effective_next_action)
        ):
            lines.append(f"pursue_next_action: {action} (superseded by next_action)")
        else:
            lines.append(f"pursue_next_action: {action}")
    return lines


def _write_project_status_text(summary):
    authoring = summary.get("authoring") if isinstance(summary.get("authoring"), dict) else {}
    latest = authoring.get("latest") or {}
    lines = [
        f"project: {summary['project']}",
        f"overall_status: {summary.get('overall_status') or summary['status']}",
        f"run_status: {summary.get('run_status') or 'none'}",
        f"active_phase: {summary.get('active_phase') or 'none'}",
        f"authoring: {authoring.get('active_count', 0)} active, {authoring.get('total_count', 0)} recorded",
    ]
    active_authoring = summary.get("active_authoring")
    if isinstance(active_authoring, dict):
        lines.append(
            "active_authoring: "
            f"{active_authoring.get('taskpack_id') or 'unknown'} "
            f"liveness={active_authoring.get('liveness_status') or 'unknown'} "
            f"elapsed_seconds={active_authoring.get('elapsed_seconds') or 0}"
        )
    if latest:
        lines.extend(
            [
                f"latest_authoring: {latest.get('taskpack_id') or 'unknown'}",
                f"liveness: {latest.get('liveness_status') or 'unknown'}",
                f"pid: {latest.get('pid') or 'unknown'}",
                f"elapsed_seconds: {latest.get('elapsed_seconds') or 0}",
                f"author_state: {latest.get('state_path') or 'unknown'}",
            ]
        )
    lines.append(f"work_root: {summary.get('work_root') or 'unknown'}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_execution_result_text(result):
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    paths = result.get("paths") if isinstance(result.get("paths"), dict) else {}
    follow_up = result.get("follow_up") if isinstance(result.get("follow_up"), dict) else {}
    lines = [
        f"status: {result.get('status') or result.get('continue_status') or 'unknown'}",
        f"taskpack_id: {result.get('taskpack_id') or 'unknown'}",
    ]
    if result.get("runtime"):
        lines.append(f"runtime: {result['runtime']}")
    if follow_up:
        lines.append(f"source_taskpack_id: {follow_up.get('source_taskpack_id') or 'unknown'}")
    repo_map_handoff_reuse = (
        result.get("repo_map_handoff_reuse")
        if isinstance(result.get("repo_map_handoff_reuse"), dict)
        else {}
    )
    if repo_map_handoff_reuse:
        reuse_line = _compact_key_value_line(
            "repo_map_handoff_reuse",
            [
                ("status", repo_map_handoff_reuse.get("status")),
                ("path", repo_map_handoff_reuse.get("handoff_path")),
                ("removed_repo_map_tasks", repo_map_handoff_reuse.get("removed_task_count")),
            ],
        )
        if reuse_line:
            lines.append(reuse_line)
    if report:
        lines.append(
            "summary: "
            f"run_status={report.get('run_status') or 'unknown'} "
            f"tasks={report.get('task_count', 0)} "
            f"blocked={report.get('blocked_count', 0)}"
        )
        if isinstance(report.get("token_usage"), dict):
            lines.append(format_token_usage(report.get("token_usage"), label="tokens"))
        completion_summary = (
            report.get("completion_summary")
            if isinstance(report.get("completion_summary"), dict)
            else {}
        )
        work_report = _compact_key_value_line(
            "work_report",
            [
                ("changed", compact_text_items(completion_summary.get("what_changed"))),
                ("files", compact_text_items(completion_summary.get("changed_files"))),
                ("verification", compact_text_items(completion_summary.get("verification"))),
                ("integration", completion_summary.get("integration")),
            ],
        )
        if work_report:
            lines.append(work_report)
        recommendation = _compact_key_value_line(
            "recommendation",
            [
                ("merge", completion_summary.get("integration_recommendation")),
                ("next", compact_text_items(completion_summary.get("next_steps"))),
                ("evidence_gap", compact_text_items(completion_summary.get("evidence_gaps"))),
            ],
        )
        if recommendation:
            lines.append(recommendation)
        if report.get("report_path"):
            lines.append(f"report: {report['report_path']}")
    run_dir = paths.get("run_dir")
    if run_dir:
        lines.append(f"run_dir: {run_dir}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _compact_key_value_line(label, items):
    parts = [
        f"{key}={value}"
        for key, value in items
        if value is not None and str(value).strip()
    ]
    if not parts:
        return None
    return f"{label}: " + "; ".join(parts)


def _artifact_snapshot_progress(snapshot):
    return f"artifact_trace: {_artifact_snapshot_text(snapshot)}"


def _artifact_snapshot_text(snapshot):
    status = snapshot.get("snapshot_status") or "unknown"
    root = snapshot.get("artifacts_root") or "unknown"
    commit = snapshot.get("commit_sha")
    if commit:
        return f"{status} commit={str(commit)[:12]} root={root}"
    return f"{status} root={root}"


def _status_run_state(snapshot, state):
    if isinstance(state, dict) and state.get("scheduler_status"):
        return state["scheduler_status"]
    tasks = snapshot.get("tasks") if isinstance(snapshot, dict) else None
    if isinstance(tasks, dict) and tasks:
        return "idle"
    return "unknown"


def _status_task_counts(snapshot, state):
    statuses = []
    tasks = snapshot.get("tasks") if isinstance(snapshot, dict) else None
    if isinstance(tasks, dict) and tasks:
        statuses = [
            task.get("task_status")
            for task in tasks.values()
            if isinstance(task, dict)
        ]
    if not statuses and isinstance(state, dict):
        backlog = state.get("backlog") if isinstance(state.get("backlog"), dict) else {}
        items = backlog.get("items") if isinstance(backlog.get("items"), list) else []
        statuses = [
            item.get("task_status") or item.get("backlog_status")
            for item in items
            if isinstance(item, dict)
        ]
    return {
        "total": len([status for status in statuses if status]),
        "done": sum(1 for status in statuses if status == "done"),
        "blocked": sum(1 for status in statuses if status == "blocked"),
        "ready": sum(1 for status in statuses if status == "ready"),
    }


_INACTIVE_INFLIGHT_RUN_STATUSES = {
    "completed",
    "failed",
    "stop_requested",
    "stopped",
    "timed_out",
}


def _status_inflight_attempts(state):
    attempts = state.get("inflight_attempts") if isinstance(state, dict) else None
    if not isinstance(attempts, list):
        return {"total": 0, "tasks": []}
    if _state_inflight_attempts_are_inactive(state):
        return {"total": 0, "tasks": []}
    return _status_attempt_summary(attempts)


def _status_inactive_inflight_attempts(state):
    attempts = state.get("inflight_attempts") if isinstance(state, dict) else None
    if not isinstance(attempts, list) or not _state_inflight_attempts_are_inactive(state):
        return {"total": 0, "tasks": []}
    return _status_attempt_summary(attempts)


def _state_inflight_attempts_are_inactive(state):
    status = str(state.get("scheduler_status") or "").strip().lower() if isinstance(state, dict) else ""
    return status in _INACTIVE_INFLIGHT_RUN_STATUSES


def _status_attempt_summary(attempts):
    tasks = [
        attempt.get("task_id")
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("task_id")
    ]
    return {"total": len(attempts), "tasks": tasks}


def _inactive_inflight_status_lines(inactive_inflight):
    if not isinstance(inactive_inflight, dict):
        return []
    total = int(inactive_inflight.get("total") or 0)
    return [f"inactive_inflight: {total}"] if total else []


def _status_evidence_counts(state):
    counts = {"complete": 0, "incomplete": 0, "blocked": 0, "escalated": 0}
    steps = state.get("steps") if isinstance(state, dict) else None
    if not isinstance(steps, list):
        return counts
    for step in steps:
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        status = result.get("evidence_status")
        if status in counts:
            counts[status] += 1
    return counts


def _status_evidence_lines(evidence):
    if not isinstance(evidence, dict):
        return []
    parts = [
        f"{status}={evidence.get(status, 0)}"
        for status in ["complete", "incomplete", "blocked", "escalated"]
        if evidence.get(status, 0)
    ]
    return [f"evidence: {', '.join(parts)}"] if parts else []


def _status_integration_counts(snapshot):
    queue = snapshot.get("integration_queue") if isinstance(snapshot, dict) else None
    if not isinstance(queue, dict):
        return {"total": 0, "blocked": 0, "verified": 0}
    latest_by_task = {}
    unbound = []
    for item in queue.values():
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        if task_id:
            # Replay preserves event order, so a later retry supersedes an
            # earlier integration outcome for operator status and gate sealing.
            latest_by_task[task_id] = item
        else:
            unbound.append(item)
    statuses = [
        item.get("queue_status") or item.get("integration_queue_status")
        for item in [*latest_by_task.values(), *unbound]
    ]
    return {
        "total": len([status for status in statuses if status]),
        "blocked": sum(1 for status in statuses if status == "blocked"),
        "verified": sum(1 for status in statuses if status in {"verified", "committed"}),
    }


def _waiting_manual_gate_count(snapshot):
    gates = snapshot.get("manual_gates") if isinstance(snapshot, dict) else None
    if not isinstance(gates, dict):
        return 0
    return sum(
        1
        for gate in gates.values()
        if isinstance(gate, dict) and gate.get("gate_status") == "waiting"
    )


def _waiting_permission_request_count(snapshot):
    requests = snapshot.get("permission_requests") if isinstance(snapshot, dict) else None
    if not isinstance(requests, dict):
        return 0
    return sum(
        1
        for request in requests.values()
        if isinstance(request, dict) and request.get("request_status") == "waiting"
    )


def _waiting_permission_request_details(snapshot, run_dir):
    requests = snapshot.get("permission_requests") if isinstance(snapshot, dict) else None
    if not isinstance(requests, dict):
        return []
    run_dir = Path(run_dir).resolve()
    details = []
    for request_id, request in sorted(requests.items()):
        if not isinstance(request, dict) or request.get("request_status") != "waiting":
            continue
        request_id = request.get("request_id") or request_id
        details.append(
            {
                "request_id": request_id,
                "task_id": request.get("task_id"),
                "attempt_id": request.get("attempt_id"),
                "requested_capability": request.get("requested_capability"),
                "request_type": request.get("request_type"),
                "reason": request.get("reason"),
                "scope": request.get("scope"),
                "sandbox": request.get("sandbox"),
                "command": request.get("command"),
                "approve_command": (
                    f"agentteam permissions approve --run-dir {run_dir} --request-id {request_id}"
                ),
                "deny_command": (
                    f"agentteam permissions deny --run-dir {run_dir} --request-id {request_id}"
                ),
            }
        )
    return details


def _status_worker_counts(worker_registry):
    workers = worker_registry.get("workers") if isinstance(worker_registry, dict) else None
    if not isinstance(workers, list):
        return {"total": 0, "stopped": 0, "running": 0, "quarantined": 0}
    statuses = [
        worker.get("worker_status")
        for worker in workers
        if isinstance(worker, dict)
    ]
    running_statuses = {"running", "started", "idle", "busy"}
    return {
        "total": len(workers),
        "stopped": sum(1 for status in statuses if status == "stopped"),
        "running": sum(1 for status in statuses if status in running_statuses),
        "quarantined": sum(1 for status in statuses if status == "quarantined"),
    }


def _status_last_worker(worker_registry):
    workers = worker_registry.get("workers") if isinstance(worker_registry, dict) else None
    if not isinstance(workers, list) or not workers:
        return None
    worker = _status_display_worker(workers)
    if not isinstance(worker, dict):
        return None
    worker_id = worker.get("worker_agent_id") or worker.get("worker_id") or "unknown-worker"
    worker_status = worker.get("worker_status") or "unknown"
    details = [f"{worker_id} {worker_status}"]
    if worker.get("worker_diagnostic_state"):
        details.append(f"diagnostic={worker['worker_diagnostic_state']}")
    if worker.get("last_activity"):
        details.append(f"activity={worker['last_activity']}")
    if worker.get("last_poll_status"):
        details.append(f"poll={worker['last_poll_status']}")
    if worker.get("heartbeat_task_id"):
        details.append(f"task={worker['heartbeat_task_id']}")
    if worker.get("heartbeat_result_status"):
        details.append(f"result={worker['heartbeat_result_status']}")
    if worker.get("heartbeat_progress_summary"):
        details.append(f"progress={worker['heartbeat_progress_summary']}")
    if worker.get("exit_code") is not None:
        details.append(f"exit_code={worker['exit_code']}")
    if worker.get("stopped_by"):
        details.append(f"stopped_by={worker['stopped_by']}")
    return " ".join(details)


def _status_display_worker(workers):
    candidates = [
        (worker, index)
        for index, worker in enumerate(workers)
        if isinstance(worker, dict)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (_status_worker_display_priority(item[0]), item[1]),
    )[0]


def _status_worker_display_priority(worker):
    status = str(worker.get("worker_status") or "").strip().lower()
    diagnostic = str(worker.get("worker_diagnostic_state") or "").strip().lower()
    activity = str(worker.get("last_activity") or "").strip().lower()
    if status in {"running", "started", "busy"}:
        return 4
    if status == "idle":
        return 3
    if diagnostic in {"processing", "processing_stale"} or activity == "processing":
        return 2
    if status == "quarantined":
        return 1
    return 0


def _status_last_failure(snapshot, state):
    attempts = snapshot.get("attempts") if isinstance(snapshot, dict) else None
    if isinstance(attempts, dict):
        for attempt in reversed(list(attempts.values())):
            if not isinstance(attempt, dict):
                continue
            failure = _attempt_failure_summary(attempt)
            if failure:
                return failure
    if isinstance(state, dict):
        for step in reversed(state.get("steps", [])):
            if isinstance(step, dict):
                failure = _attempt_failure_summary(step.get("result", {}))
                if failure:
                    return failure
    return None


def _attempt_failure_summary(attempt):
    if not isinstance(attempt, dict):
        return None
    stderr = attempt.get("integration_verification_stderr") or attempt.get("stderr") or ""
    if isinstance(stderr, str):
        for line in stderr.splitlines():
            stripped = line.strip()
            if "ModuleNotFoundError" in stripped or "FAILED" in stripped:
                return stripped
    for key in ["failure_category", "integration_verification_status", "validation_status"]:
        value = attempt.get(key)
        if value and value not in {"accepted", "completed"}:
            return str(value)
    return None


def _waiting_manual_gates_summary(run_dir, waiting_gates, resume_context=None):
    return {
        "resume_status": "waiting_manual_gates",
        "waiting_count": len(waiting_gates),
        "waiting": [
            _manual_gate_summary_item(gate, resume_context or {})
            for gate in waiting_gates
        ],
        "run_dir": str(Path(run_dir).resolve()),
    }


def _manual_gate_summary_item(gate, resume_context):
    task = _task_for_gate(gate, resume_context)
    item = {
        "question_id": gate.get("question_id"),
        "task_id": gate.get("task_id"),
        "attempt_id": gate.get("attempt_id"),
        "question": gate.get("question"),
        "options": gate.get("options", []),
        "reason": gate.get("reason"),
    }
    if task:
        item["objective"] = task.get("objective")
        item["risk_target"] = task.get("risk_target")
        item["backlog_status"] = task.get("backlog_status")
    return item


def _progress(enabled, message):
    if enabled:
        _write_progress(message)


def _author_progress_callback(enabled):
    if not enabled:
        return None

    def emit(state):
        status = state.get("author_status") or "unknown"
        taskpack_id = state.get("taskpack_id") or "unknown"
        elapsed = int(float(state.get("elapsed_seconds") or 0))
        pid = state.get("pid") or "unknown"
        state_path = state.get("state_path")
        message = f"authoring status={status} taskpack={taskpack_id} elapsed={elapsed}s pid={pid}"
        if state_path:
            message += f" state={state_path}"
        _write_progress(message)

    return emit


def _write_progress(message):
    sys.stderr.write(f"[agentteam] {message}\n")
    sys.stderr.flush()


def _run_progress_status(run):
    if not isinstance(run, dict):
        return "completed"
    for key in ["scheduler_status", "daemon_status", "status"]:
        value = run.get(key)
        if value:
            return str(value)
    return _submit_status_from_run(run)


def _submit_status_from_run(run):
    if not isinstance(run, dict):
        return "completed"
    if run.get("scheduler_status") == "awaiting_post_backlog_gates":
        return "awaiting_post_backlog_gates"
    snapshot = run.get("snapshot")
    if not isinstance(snapshot, dict):
        return "completed"
    manual_gates = snapshot.get("manual_gates", {})
    if isinstance(manual_gates, dict) and any(
        gate.get("gate_status") == "waiting"
        for gate in manual_gates.values()
        if isinstance(gate, dict)
    ):
        return "manual_gate_required"
    permission_requests = snapshot.get("permission_requests", {})
    if isinstance(permission_requests, dict) and any(
        request.get("request_status") == "waiting"
        for request in permission_requests.values()
        if isinstance(request, dict)
    ):
        return "permission_request_required"
    tasks = snapshot.get("tasks", {})
    if isinstance(tasks, dict) and any(
        task.get("task_status") == "blocked"
        for task in tasks.values()
        if isinstance(task, dict)
    ):
        return "blocked"
    return "completed"


def _waiting_manual_gates(run_dir):
    snapshot = _load_resume_context(run_dir)["snapshot"]
    return _waiting_manual_gates_from_snapshot(snapshot)


def _load_resume_context(run_dir):
    run_dir = Path(run_dir)
    events_path = run_dir / "events.jsonl"
    return {
        "run_dir": run_dir,
        "events": _read_jsonl(events_path),
        "snapshot": replay_events(events_path),
        "state": _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json"),
    }


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


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _waiting_manual_gates_from_snapshot(snapshot):
    manual_gates = snapshot.get("manual_gates", {})
    if not isinstance(manual_gates, dict):
        return []
    return [
        gate
        for _question_id, gate in sorted(manual_gates.items())
        if isinstance(gate, dict) and gate.get("gate_status") == "waiting"
    ]


def _selected_waiting_manual_gates(question_id, waiting_gates):
    if not question_id:
        return waiting_gates
    for gate in waiting_gates:
        if gate.get("question_id") == question_id:
            return [gate]
    raise AgentTeamCliError(
        "manual gate question id is not waiting",
        question_id=question_id,
        waiting_question_ids=[
            gate.get("question_id")
            for gate in waiting_gates
            if gate.get("question_id")
        ],
    )


def _prompt_manual_gate_answer(gate, resume_context=None):
    resume_context = resume_context or {"events": [], "state": {}}
    _write_manual_gate_header(gate)
    _write_manual_gate_commands()
    while True:
        sys.stderr.write("Answer or command: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if line == "":
            raise AgentTeamCliError(
                "interactive input ended before manual gate was answered",
                question_id=gate.get("question_id"),
            )
        value = line.strip()
        if not value:
            sys.stderr.write("Answer is required.\n")
            sys.stderr.flush()
            continue
        if not value.startswith("/"):
            return value
        command, _separator, argument = value.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command == "/answer":
            if argument:
                return argument
            return _prompt_text("Final answer", required=True)
        if command in {"/help", "/?"}:
            _write_manual_gate_commands()
        elif command in {"/gates", "/list"}:
            _write_waiting_manual_gates(resume_context)
        elif command == "/task":
            _write_manual_gate_task(gate, resume_context)
        elif command == "/why":
            _write_manual_gate_why(gate)
        elif command == "/events":
            _write_manual_gate_events(gate, resume_context)
        elif command == "/context":
            _write_manual_gate_task(gate, resume_context)
            _write_manual_gate_why(gate)
            _write_manual_gate_events(gate, resume_context)
        else:
            sys.stderr.write(f"Unknown command: {command}\n")
            _write_manual_gate_commands()
        sys.stderr.flush()


def _write_manual_gate_header(gate):
    sys.stderr.write(f"Manual gate {gate['question_id']}\n")
    task_id = gate.get("task_id")
    if task_id:
        sys.stderr.write(f"Task: {task_id}\n")
    question = gate.get("question") or "Worker requested operator guidance before continuing."
    sys.stderr.write(f"Question: {question}\n")
    options = gate.get("options") or []
    if options:
        sys.stderr.write(f"Options: {', '.join(options)}\n")
    reason = gate.get("reason")
    if reason:
        sys.stderr.write(f"Reason: {reason}\n")
    sys.stderr.flush()


def _write_manual_gate_commands():
    sys.stderr.write(
        "Commands: /gates, /task, /why, /events, /context, /answer <text>, /help. "
        "Plain text also submits the answer.\n"
    )
    sys.stderr.flush()


def _write_waiting_manual_gates(resume_context):
    snapshot = resume_context.get("snapshot", {}) if isinstance(resume_context, dict) else {}
    waiting_gates = _waiting_manual_gates_from_snapshot(snapshot)
    sys.stderr.write("Waiting manual gates:\n")
    if not waiting_gates:
        sys.stderr.write("- No waiting manual gates.\n")
        return
    for gate in waiting_gates:
        question_id = gate.get("question_id") or "unknown"
        task_id = gate.get("task_id") or "unknown"
        question = gate.get("question") or "Worker requested operator guidance before continuing."
        task = _task_for_gate(gate, resume_context)
        risk = f" risk={task['risk_target']}" if task and task.get("risk_target") else ""
        objective = f" objective={task['objective']}" if task and task.get("objective") else ""
        sys.stderr.write(f"- {question_id} task={task_id}{risk}{objective} question={question}\n")


def _write_manual_gate_task(gate, resume_context):
    task = _task_for_gate(gate, resume_context)
    sys.stderr.write("Task context:\n")
    if not task:
        task_id = gate.get("task_id") or "unknown"
        sys.stderr.write(f"- Task id: {task_id}\n")
        sys.stderr.write("- Scheduler task state was not found.\n")
        return
    fields = [
        ("Task id", task.get("task_id")),
        ("Status", task.get("backlog_status") or task.get("task_status")),
        ("Milestone", task.get("milestone_id")),
        ("Objective", task.get("objective")),
        ("Risk", task.get("risk_target")),
        ("Required role", task.get("required_role")),
        ("Read scope", _compact_list(task.get("read_scope"))),
        ("Write scope", _compact_list(task.get("write_scope"))),
        ("Blockers", _compact_list(task.get("blockers"))),
    ]
    for label, value in fields:
        if value:
            sys.stderr.write(f"- {label}: {value}\n")


def _write_manual_gate_why(gate):
    sys.stderr.write("Gate reason:\n")
    question = gate.get("question") or "Worker requested operator guidance before continuing."
    sys.stderr.write(f"- Question: {question}\n")
    options = gate.get("options") or []
    if options:
        sys.stderr.write(f"- Options: {', '.join(str(option) for option in options)}\n")
    reason = gate.get("reason")
    if reason:
        sys.stderr.write(f"- Reason: {reason}\n")


def _write_manual_gate_events(gate, resume_context, limit=8):
    events = _related_events(gate, resume_context, limit=limit)
    sys.stderr.write("Recent related events:\n")
    if not events:
        sys.stderr.write("- No related events found.\n")
        return
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        details = [
            f"event={event.get('event_type') or 'unknown'}",
            f"sequence={event.get('sequence')}",
        ]
        task_id = payload.get("task_id")
        attempt_id = payload.get("attempt_id")
        question_id = payload.get("question_id")
        if task_id:
            details.append(f"task={task_id}")
        if attempt_id:
            details.append(f"attempt={attempt_id}")
        if question_id:
            details.append(f"question={question_id}")
        sys.stderr.write(f"- {' '.join(str(detail) for detail in details if detail)}\n")


def _task_for_gate(gate, resume_context):
    task_id = gate.get("task_id")
    if not task_id:
        return None
    state = resume_context.get("state") if isinstance(resume_context, dict) else {}
    backlog = state.get("backlog") if isinstance(state, dict) else {}
    items = backlog.get("items") if isinstance(backlog, dict) else []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("task_id") == task_id:
                return item
    return None


def _related_events(gate, resume_context, limit=8):
    task_id = gate.get("task_id")
    question_id = gate.get("question_id")
    events = resume_context.get("events", []) if isinstance(resume_context, dict) else []
    related = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if (task_id and payload.get("task_id") == task_id) or (
            question_id and payload.get("question_id") == question_id
        ):
            related.append(event)
    return related[-limit:]


def _compact_list(value):
    if not value:
        return None
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else None
    return str(value)


def _profile_from_args(args, project_root):
    return build_project_profile(
        project_root,
        project_key=args.project_key,
        work_root=args.work_root,
        author_runtime=args.author_runtime,
        default_runtime=args.runtime,
        codex_model=getattr(args, "codex_model", None),
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
        notification_project=args.notification_project,
        feishu_enabled=bool(args.feishu_webhook_env),
        feishu_webhook_env=args.feishu_webhook_env,
        feishu_signing_secret_env=args.feishu_signing_secret_env,
        verification_profile=_verification_profile_from_args(args),
    )


def _prompt_project_profile(args, project_root):
    project_key = _prompt_text(
        "Project key",
        default=args.project_key or default_project_key(project_root),
        required=True,
    )
    work_root = _prompt_text(
        "Work root",
        default=args.work_root or str(default_work_root(project_key)),
        required=True,
    )
    author_runtime = _prompt_choice(
        "Author runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        default=args.author_runtime or "codex",
    )
    runtime = _prompt_choice(
        "Runtime",
        choices=["auto", "fake", "codex"],
        default=args.runtime or "auto",
    )
    codex_model = _prompt_text(
        "Codex model",
        default=getattr(args, "codex_model", None),
        display_default="default",
        required=False,
    )
    one_shot = _prompt_bool("One shot", default=bool(args.one_shot))
    commit_verified_integration = _prompt_bool(
        "Commit verified integration",
        default=bool(args.commit_verified_integration),
    )
    feishu_enabled = _prompt_bool("Enable Feishu notifications", default=bool(args.feishu_webhook_env))
    feishu_webhook_env = None
    feishu_signing_secret_env = None
    if feishu_enabled:
        env_prefix = project_key.upper().replace("-", "_").replace(".", "_")
        feishu_webhook_env = _prompt_text(
            "Feishu webhook env",
            default=args.feishu_webhook_env or f"AGENTTEAM_FEISHU_{env_prefix}_WEBHOOK",
            required=True,
        )
        feishu_signing_secret_env = _prompt_text(
            "Feishu signing secret env",
            default=args.feishu_signing_secret_env,
            display_default="none",
            required=False,
        )
    return build_project_profile(
        project_root,
        project_key=project_key,
        work_root=work_root,
        author_runtime=author_runtime,
        default_runtime=runtime,
        codex_model=codex_model,
        one_shot=one_shot,
        max_inflight=args.max_inflight or 2,
        max_attempts=args.max_attempts or 1,
        commit_verified_integration=commit_verified_integration,
        notification_project=args.notification_project,
        feishu_enabled=feishu_enabled,
        feishu_webhook_env=feishu_webhook_env,
        feishu_signing_secret_env=feishu_signing_secret_env,
        verification_profile=_verification_profile_from_args(args),
    )


def _submit_args_from_profile(args, project_root, profile):
    feishu = profile.get("feishu") if isinstance(profile.get("feishu"), dict) else {}
    feishu_enabled = bool(feishu.get("enabled"))
    return argparse.Namespace(
        interactive=False,
        project_root=str(project_root),
        goal=args.goal,
        work_root=args.work_root or profile.get("work_root"),
        taskpack_id=args.taskpack_id,
        author_runtime=args.author_runtime or profile.get("author_runtime", "codex"),
        runtime=args.runtime or profile.get("default_runtime", "auto"),
        codex_timeout_seconds=args.codex_timeout_seconds,
        codex_model=getattr(args, "codex_model", None) or profile.get("codex_model"),
        one_shot=_override_or_profile(args.one_shot, profile.get("one_shot", False)),
        max_inflight=args.max_inflight or profile.get("max_inflight", 2),
        max_attempts=args.max_attempts or profile.get("max_attempts", 1),
        commit_verified_integration=_override_or_profile(
            args.commit_verified_integration,
            profile.get("commit_verified_integration", False),
        ),
        notification_project=args.notification_project
        or profile.get("notification_project")
        or profile.get("project_key")
        or "agentteam",
        feishu_webhook_env=args.feishu_webhook_env
        if args.feishu_webhook_env is not None
        else (feishu.get("webhook_env") if feishu_enabled else None),
        feishu_signing_secret_env=args.feishu_signing_secret_env
        if args.feishu_signing_secret_env is not None
        else (feishu.get("signing_secret_env") if feishu_enabled else None),
        codex_command=args.codex_command,
        initial_integration_base_ref=getattr(args, "initial_integration_base_ref", None),
        verification_profile=effective_project_verification_profile(
            project_root,
            profile.get("verification_profile"),
        ),
    )


def _override_or_profile(override, profile_value):
    return profile_value if override is None else override


def _prepare_bound_implementation_run(
    loaded_taskpack,
    *,
    frozen_taskpack_dir,
    run_paths,
    work_root,
):
    """Publish or validate the launcher-selected pair before any runtime child."""
    selection = _launcher_runtime_selection()
    if not selection:
        return None
    work_root = Path(work_root).resolve()
    selected_work_root = Path(selection["work_root"]).resolve()
    run_dir = Path(run_paths["run_dir"]).resolve()
    if work_root != selected_work_root:
        raise AgentTeamCliError(
            "runtime work root does not match launcher selection",
            runtime_work_root=str(work_root),
            launcher_work_root=str(selected_work_root),
        )
    selected_run_dir = selection.get("run_dir")
    if selected_run_dir and Path(selected_run_dir).resolve() != run_dir:
        raise AgentTeamCliError(
            "runtime run directory does not match launcher selection",
            runtime_run_dir=str(run_dir),
            launcher_run_dir=str(Path(selected_run_dir).resolve()),
        )
    taskpack_id = loaded_taskpack.get("taskpack_id") or Path(frozen_taskpack_dir).name
    context = (
        loaded_taskpack.get("context")
        if isinstance(loaded_taskpack.get("context"), dict)
        else {}
    )
    expected = {
        key: value
        for key, value in {
            "release_id": context.get("runtime_release_id"),
            "source_commit": context.get("runtime_release_source_commit"),
            "git_object_format": context.get("git_object_format"),
        }.items()
        if value is not None
    }
    launcher_expected = selection.get("expected_release") or {}
    if expected != launcher_expected:
        raise AgentTeamCliError(
            "frozen taskpack release expectation does not match launcher selection"
        )
    if run_dir.exists():
        pair = validate_run_binding(
            run_dir,
            expected_project_key=selection.get("project_key"),
            expected_release=expected,
            expected_identity_sha256=selection.get("identity_sha256"),
        )
    else:
        pair = publish_implementation_run(
            work_root,
            project_key=selection.get("project_key") or work_root.name,
            run_id=run_dir.name,
            taskpack_id=taskpack_id,
            release_identity=selection["release"],
            expected_release=expected,
        )
    updated_selection = {
        **selection,
        "run_dir": pair["run_dir"],
        "identity_sha256": pair["identity_sha256"],
        "selection_mode": selection.get("selection_mode") or "runtime_validated",
    }
    os.environ[_LAUNCHER_SELECTION_ENV] = json.dumps(
        updated_selection,
        sort_keys=True,
        separators=(",", ":"),
    )
    return pair


def _complete_submit_args(args):
    if args.interactive:
        _prompt_submit_args(args)
        return

    _require_submit_arg(args.project_root, "--project-root")
    _require_submit_arg(args.goal, "--goal")
    _require_submit_arg(args.work_root, "--work-root")


def _prompt_submit_args(args):
    args.project_root = _prompt_text("Project root", default=args.project_root, required=True)
    args.goal = _prompt_text("Goal", default=args.goal, required=True)
    args.work_root = _prompt_text(
        "Work root",
        default=args.work_root or "/tmp/agentteam-taskpacks",
        required=True,
    )
    args.taskpack_id = _prompt_text(
        "Taskpack id",
        default=args.taskpack_id,
        display_default="auto" if args.taskpack_id is None else None,
        required=False,
    )
    args.author_runtime = _prompt_choice(
        "Author runtime",
        choices=AUTHOR_RUNTIME_CHOICES,
        default=args.author_runtime,
    )
    args.runtime = _prompt_choice(
        "Runtime",
        choices=["auto", "fake", "codex"],
        default=args.runtime,
    )
    args.one_shot = _prompt_bool("One shot", default=True)
    args.commit_verified_integration = _prompt_bool(
        "Commit verified integration",
        default=args.commit_verified_integration,
    )


def _require_submit_arg(value, flag):
    if value:
        return
    raise AgentTeamCliError(f"{flag} is required unless --interactive is set", missing_argument=flag)


def _prompt_text(label, default=None, display_default=None, required=False):
    shown_default = display_default if display_default is not None else default
    while True:
        suffix = f" [{shown_default}]" if shown_default else ""
        sys.stderr.write(f"{label}{suffix}: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if line == "":
            raise AgentTeamCliError("interactive input ended before submit was complete", prompt=label)
        value = line.strip()
        if value:
            return value
        if default is not None or not required:
            return default
        sys.stderr.write(f"{label} is required.\n")
        sys.stderr.flush()


def _prompt_choice(label, choices, default):
    choices_label = "/".join(choices)
    while True:
        value = _prompt_text(f"{label} ({choices_label})", default=default, required=True)
        if value in choices:
            return value
        sys.stderr.write(f"{label} must be one of: {choices_label}.\n")
        sys.stderr.flush()


def _prompt_bool(label, default):
    default_label = "Y/n" if default else "y/N"
    while True:
        sys.stderr.write(f"{label} [{default_label}]: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if line == "":
            raise AgentTeamCliError("interactive input ended before submit was complete", prompt=label)
        value = line.strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "true", "1"}:
            return True
        if value in {"n", "no", "false", "0"}:
            return False
        sys.stderr.write(f"{label} must be y or n.\n")
        sys.stderr.flush()


def _run_frozen_taskpack(
    frozen_taskpack_dir,
    run_root,
    one_shot=False,
    max_inflight=2,
    max_attempts=1,
    commit_verified_integration=False,
    notification_project="agentteam",
    feishu_webhook_env=None,
    feishu_signing_secret_env=None,
    progress=False,
    progress_interval_seconds=2.0,
    initial_integration_base_ref=None,
):
    loaded_taskpack = load_taskpack(frozen_taskpack_dir)["taskpack"]
    post_backlog_gates = loaded_taskpack.get("post_backlog_gates")
    if one_shot and isinstance(post_backlog_gates, list) and post_backlog_gates:
        raise AgentTeamCliError(
            "--one-shot cannot launch a taskpack with post-backlog gates; "
            "use the daemon worker-pool path so external controller gates can run",
            taskpack_id=loaded_taskpack.get("taskpack_id"),
            post_backlog_gate_ids=[
                gate.get("gate_id")
                for gate in post_backlog_gates
                if isinstance(gate, dict)
            ],
        )
    run_paths = _run_paths_for_frozen_taskpack(frozen_taskpack_dir, run_root)
    inferred_work_root = _infer_work_root_for_run(
        run_paths["run_root"],
        frozen_taskpack_dir,
    )
    _prepare_bound_implementation_run(
        loaded_taskpack,
        frozen_taskpack_dir=Path(frozen_taskpack_dir).resolve(),
        run_paths=run_paths,
        work_root=inferred_work_root,
    )
    runtime_args = build_taskpack_runtime_args(
        frozen_taskpack_dir,
        run_root=run_paths["run_root"],
        daemon=not one_shot,
        max_inflight=max_inflight,
        max_attempts=max_attempts,
        commit_verified_integration=commit_verified_integration,
        initial_integration_base_ref=initial_integration_base_ref,
    )
    _initialize_post_backlog_gate_state(
        {"work_root": str(inferred_work_root)},
        run_paths["run_dir"],
    )
    if notification_project:
        runtime_args.extend(["--notification-project", notification_project])
    if feishu_webhook_env:
        runtime_args.extend(["--feishu-webhook-env", feishu_webhook_env])
    if feishu_signing_secret_env:
        runtime_args.extend(["--feishu-signing-secret-env", feishu_signing_secret_env])
    command = [sys.executable, "-m", "agentteam_runtime.cli", *runtime_args]
    env = _runtime_subprocess_env()
    return _run_runtime_command_with_progress(
        command,
        env=env,
        run_dir=run_paths["run_dir"],
        progress=progress,
        progress_interval_seconds=progress_interval_seconds,
        progress_stream=sys.stderr,
    )


def _run_runtime_command_with_progress(
    command,
    env,
    run_dir,
    progress=False,
    progress_interval_seconds=2.0,
    progress_stream=None,
):
    if not progress:
        return subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    progress_stream = progress_stream or sys.stderr
    run_dir = Path(run_dir)
    interval = max(float(progress_interval_seconds or 0), 0.05)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                command,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            cursor = _runtime_progress_cursor()
            next_emit_at = 0.0
            while True:
                returncode = process.poll()
                now = time.monotonic()
                if now >= next_emit_at:
                    cursor = _emit_runtime_progress(run_dir, cursor, progress_stream)
                    next_emit_at = now + interval
                if returncode is not None:
                    break
                time.sleep(min(interval, 0.2))
            _emit_runtime_progress(run_dir, cursor, progress_stream)
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout_file.read(),
                stderr_file.read(),
            )


def _emit_runtime_progress(run_dir, cursor, progress_stream):
    run_dir = Path(run_dir)
    events_path = run_dir / "events.jsonl"
    cursor = _runtime_progress_cursor(cursor)
    try:
        event_cursor, events = read_event_records_since(
            events_path,
            cursor["event_cursor"],
            max_records=50,
        )
    except (OSError, json.JSONDecodeError):
        event_cursor = cursor["event_cursor"]
        events = []
    try:
        snapshot = replay_events(events_path) if events_path.exists() else {}
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        snapshot = {}
    state = _read_json_progress_safe(run_dir / "state" / "two_phase_scheduler_state.json")
    worker_registry = _read_json_progress_safe(run_dir / "state" / "worker_process_registry.json")
    if not worker_registry:
        worker_registry = _read_json_progress_safe(run_dir / "state" / "worker_registry.json")
    task_counts = _status_task_counts(snapshot, state)
    inflight = _status_inflight_attempts(state)
    workers = _status_worker_counts(worker_registry)
    event_type = events[-1].get("event_type") if events else None
    summary_pieces = [
        f"status={_status_run_state(snapshot, state)}",
        f"tasks={task_counts['done']}/{task_counts['total']}",
        f"blocked={task_counts['blocked']}",
        f"inflight={inflight['total']}",
        f"manual_gates={_waiting_manual_gate_count(snapshot)}",
        f"permission_requests={_waiting_permission_request_count(snapshot)}",
    ]
    if workers["total"]:
        summary_pieces.append(
            f"workers={workers['running']} running/{workers['stopped']} stopped"
        )
    summary_key = tuple(summary_pieces)
    cursor["event_cursor"] = event_cursor
    should_emit = summary_key != cursor["last_summary_key"]
    if not should_emit:
        return cursor
    pieces = list(summary_pieces)
    if event_type:
        pieces.append(f"event={event_type}")
    _write_progress_to_stream(f"runtime {' '.join(pieces)}", progress_stream)
    cursor["last_summary_key"] = summary_key
    return cursor


def _runtime_progress_cursor(cursor=None):
    if isinstance(cursor, dict):
        return {
            "event_cursor": cursor.get("event_cursor", 0),
            "last_summary_key": cursor.get("last_summary_key"),
        }
    return {"event_cursor": cursor or 0, "last_summary_key": None}


def _write_progress_to_stream(message, stream):
    stream.write(f"[agentteam] {message}\n")
    stream.flush()


def _read_json_progress_safe(path):
    try:
        return _read_json_if_exists(path)
    except (OSError, json.JSONDecodeError):
        return {}


def _runtime_subprocess_env():
    env = os.environ.copy()
    selection = _launcher_runtime_selection()
    runtime_root = str(
        Path(selection["release"]["runtime_root"]).resolve()
        if selection
        else Path(__file__).resolve().parents[1]
    )
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = runtime_root if not current else f"{runtime_root}:{current}"
    return env


def _submit_runtime_backend(runtime, author_runtime):
    if runtime != "auto":
        return runtime
    return "fake" if author_runtime == "fake" else "codex"


def _set_taskpack_runtime_backend(taskpack_dir, runtime_backend):
    taskpack_dir = Path(taskpack_dir)
    taskpack_path = taskpack_dir / "taskpack.yaml"
    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
    runtime = taskpack.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    runtime["default_backend"] = runtime_backend
    taskpack["runtime"] = runtime
    _write_json(taskpack_path, taskpack)

    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        files = {}

    agent_pool_path = taskpack_dir / files.get("agent_pool", "agent_pool.json")
    agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
    for profile in _runtime_profiles(agent_pool):
        profile["adapter"] = runtime_backend
    _write_json(agent_pool_path, agent_pool)

    if runtime_backend == "fake":
        backlog_path = taskpack_dir / files.get("backlog", "backlog.json")
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        if isinstance(backlog, dict):
            for item in backlog.get("items", []):
                if isinstance(item, dict):
                    item["write_scope"] = ["generated/"]
        _write_json(backlog_path, backlog)


def _set_taskpack_codex_model(taskpack_dir, codex_model, runtime_backend="codex"):
    if runtime_backend != "codex" or not codex_model:
        return
    if not isinstance(codex_model, str) or not codex_model.strip():
        raise AgentTeamCliError("codex_model must be a non-empty string")
    codex_model = codex_model.strip()
    taskpack_dir = Path(taskpack_dir)
    taskpack_path = taskpack_dir / "taskpack.yaml"
    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
    runtime = taskpack.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    codex = runtime.get("codex")
    if not isinstance(codex, dict):
        codex = {}
    codex["model"] = codex_model
    runtime["codex"] = codex
    taskpack["runtime"] = runtime
    _write_json(taskpack_path, taskpack)

    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        files = {}
    agent_pool_path = taskpack_dir / files.get("agent_pool", "agent_pool.json")
    agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
    for profile in _runtime_profiles(agent_pool):
        profile["model"] = codex_model
    _write_json(agent_pool_path, agent_pool)


def _runtime_profiles(agent_pool):
    if not isinstance(agent_pool, dict):
        return
    role_profiles = agent_pool.get("role_runtime_profiles")
    if isinstance(role_profiles, dict):
        for profile in role_profiles.values():
            if isinstance(profile, dict):
                yield profile
    agents = agent_pool.get("agents")
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict) and isinstance(agent.get("runtime_profile"), dict):
                yield agent["runtime_profile"]


def _json_or_output(stdout):
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"stdout": stdout}


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_json(payload, stream):
    print(json.dumps(payload, sort_keys=True), file=stream)


def _error_payload(exc):
    payload = {
        "status": "error",
        "error": str(exc),
        "error_type": exc.__class__.__name__,
    }
    if isinstance(exc, AgentTeamCliError):
        payload.update(exc.details)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
    render_run_completion_report,
)
from .profile import (
    AgentTeamProfileError,
    build_project_profile,
    default_project_key,
    default_work_root,
    load_project_profile,
    profile_path_for_project,
    write_project_profile,
)
from .release_manager import (
    activate_release,
    install_release_from_checkout,
    record_active_release_for_run,
    prune_releases,
    update_status,
)
from .notifications import FeishuWebhookNotifier
from .taskpack import (
    build_taskpack_runtime_args,
    draft_taskpack_files,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
from .taskpack_author import draft_taskpack_from_goal
from .token_usage import format_token_usage, token_usage_from_state


class AgentTeamCliError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentTeamCliError(message)


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
        "name": "status",
        "summary": "Show the latest run state, including liveness and workers.",
        "examples": ["agentteam status --project-root <repo>"],
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
            "agentteam notify run-completed --project-root <repo> --taskpack <id>",
        ],
        "subcommands": [
            "test: send a diagnostic Feishu notification from the current project profile",
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
        "summary": "Draft, validate, freeze, list, and delete taskpacks.",
        "examples": [
            "agentteam taskpack new --goal \"profile algorithm latency\" --write-scope output/current/",
            "agentteam taskpack new --goal \"profile algorithm latency\" --write-scope output/current/ --freeze",
            "agentteam taskpack list --project-root <repo>",
            "agentteam taskpack delete --project-root <repo> --taskpack <id> --dry-run",
            "agentteam taskpack delete --project-root <repo> --taskpack <id> --delete-run --force",
        ],
        "subcommands": [
            "new: create an explicit operator-authored taskpack from project profile defaults",
            "draft: create a taskpack from a goal without running it",
            "validate: validate a draft or frozen taskpack",
            "freeze: freeze an accepted draft for execution",
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
    _add_taskpack_new_parser(taskpack_subcommands)
    _add_taskpack_draft_parser(taskpack_subcommands)
    _add_taskpack_validate_parser(taskpack_subcommands)
    _add_taskpack_freeze_parser(taskpack_subcommands)
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
    _add_stop_parser(subcommands)
    _add_update_parser(subcommands)
    _add_paths_parser(subcommands)
    _add_integrate_parser(subcommands)
    _add_notify_parser(subcommands)
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
        choices=["fake", "codex"],
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
        choices=["fake", "codex"],
        default="codex",
        help="Runtime used to author taskpacks for this project.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        default="auto",
        help="Runtime backend used to execute taskpacks for this project.",
    )
    parser.add_argument("--one-shot", action="store_true", help="Default to one-shot runtime execution.")
    parser.add_argument("--max-inflight", type=int, default=2, help="Default maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Default maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Default to committing integration worktree changes after verification passes.",
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
        choices=["fake", "codex"],
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
        choices=["fake", "codex"],
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
        choices=["fake", "codex"],
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


def _add_taskpack_validate_parser(subcommands):
    parser = subcommands.add_parser("validate", help="Validate a draft or frozen taskpack.")
    parser.add_argument("taskpack_dir", help="Taskpack directory to validate.")
    parser.set_defaults(handler=_handle_taskpack_validate)


def _add_taskpack_freeze_parser(subcommands):
    parser = subcommands.add_parser("freeze", help="Freeze an accepted taskpack for runtime launch.")
    parser.add_argument("taskpack_dir", help="Draft taskpack directory to freeze.")
    parser.add_argument("--frozen-root", required=True, help="Directory where frozen taskpacks are written.")
    parser.set_defaults(handler=_handle_taskpack_freeze)


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


def _add_paths_parser(subcommands):
    parser = subcommands.add_parser("paths", help="Show AgentTeam project and run paths.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to inspect. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to inspect. Overrides --project-root selection.")
    parser.add_argument("--json", action="store_true", help="Print paths as JSON instead of human text.")
    parser.set_defaults(handler=_handle_paths)


def _add_integrate_parser(subcommands):
    parser = subcommands.add_parser(
        "integrate",
        help="Fast-forward a run integration baseline into the target repository.",
    )
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    parser.add_argument("--taskpack", help="Run/taskpack id to integrate. Defaults to latest run id.")
    parser.add_argument("--run-dir", help="Existing run directory to integrate. Overrides --taskpack.")
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


def _add_update_parser(subcommands):
    parser = subcommands.add_parser("update", help="Manage side-by-side AgentTeam runtime releases.")
    parser.add_argument("--project-root", help="Git repository root for the target project. Defaults to cwd.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Show active release, known releases, and run bindings.")
    action.add_argument("--from", dest="source_checkout", help="Install and activate a release from a clean checkout.")
    action.add_argument("--activate", help="Activate an already installed release id.")
    action.add_argument("--rollback", help="Activate an older release id.")
    action.add_argument("--prune", action="store_true", help="Prune old installed releases, keeping the active/latest release.")
    parser.add_argument("--release-id", help="Release id to use with --from. Defaults to git commit.")
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
    verification_command = _parse_verification_command_arg(
        args.verification_command_json,
        default=["python3", "-m", "unittest", "discover"],
    )
    draft = draft_taskpack_files(
        project_root=project_root,
        goal=goal,
        draft_root=draft_root,
        taskpack_id=args.taskpack_id,
        read_scope=args.read_scope or ["."],
        write_scope=write_scope,
        verification_command=verification_command,
        allow_merge=args.allow_merge,
        codex_timeout_seconds=args.codex_timeout_seconds,
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


def _frozen_taskpack_list_summary(profile):
    work_root = Path(profile["work_root"]).resolve()
    frozen_root = work_root / "frozen"
    run_root = work_root / "runs"
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
        "taskpacks": taskpacks,
    }


def _write_taskpack_list_text(summary):
    lines = [
        f"project: {summary['project']}",
        f"frozen_count: {summary['frozen_count']}",
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

    _write_progress(f"profile loaded: {profile.get('project_key') or project_root.name}")
    _write_progress(f"follow-up source: {source_run_dir.name}")
    submit_args = _submit_args_from_profile(args, project_root, profile)
    submit_args.goal = followup_goal
    submit_args.progress = True
    result = _handle_submit(submit_args)
    result["follow_up"] = {
        "source_taskpack_id": source_run_dir.name,
        "source_run_dir": str(source_run_dir),
        "source_report_path": source_report["report_path"],
        "requested_goal": requested_goal,
    }
    if args.json:
        return result
    _write_execution_result_text(result)
    return 0


def _followup_source_run_dir(args, profile, work_root):
    if args.from_run_dir:
        return Path(args.from_run_dir).resolve()
    if args.from_taskpack:
        return (work_root / "runs" / args.from_taskpack).resolve()
    return _latest_run_dir(profile)


def _build_followup_goal(requested_goal, source_report):
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
    return "\n".join(lines)


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
        progress_callback=_author_progress_callback(progress),
    )
    _progress(progress, f"draft accepted: {draft['taskpack_id']}")
    taskpack_dir = Path(draft["taskpack_dir"])
    _set_taskpack_runtime_backend(taskpack_dir, runtime_backend)
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
    report = build_run_completion_report(
        run_dir,
        project=args.notification_project or "agentteam",
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
    return {
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
    report = build_run_completion_report(
        run_dir,
        project=args.notification_project or "agentteam",
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
    report = build_run_completion_report(
        run_dir,
        project=profile.get("project_key") or "agentteam",
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


def _handle_status(args):
    project_root = Path(args.project_root or ".").resolve()
    profile = load_project_profile(project_root)
    try:
        run_dir = _canonical_run_dir(Path(args.run_dir).resolve()) if args.run_dir else _latest_run_dir(profile)
        summary = _build_run_status_summary(profile, run_dir)
    except AgentTeamCliError as exc:
        authoring = _build_project_authoring_summary(profile)
        if not args.run_dir and authoring["active_count"]:
            summary = _build_project_status_summary(profile, authoring)
        else:
            raise exc
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
    summary = _integrate_run_baseline(project_root, profile, run_dir)
    if args.json:
        return summary
    _write_integrate_text(summary)
    return 0


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
    else:
        raise AgentTeamCliError("update action is required")
    summary = _attach_release_status_fields(summary, profile)
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
    report = build_run_completion_report(
        run_dir,
        project=profile.get("project_key") or "unknown",
    )
    artifact_snapshot = snapshot_run_artifacts_safe(
        profile.get("work_root") or run_dir.parent,
        run_dir,
        taskpack_id=run_dir.name,
        project=profile.get("project_key") or "unknown",
    )
    if args.json:
        report = {**report, "artifact_snapshot": artifact_snapshot}
        return report
    sys.stdout.write(render_run_completion_report(report))
    sys.stdout.flush()
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


def _watch_profile(args):
    if args.project_root:
        return load_project_profile(Path(args.project_root).resolve())
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        work_root = run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir.parent
        return {
            "project_key": "unknown",
            "work_root": str(work_root),
        }
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


def _attach_release_status_fields(summary, profile):
    status = update_status(profile)
    enriched = dict(summary)
    for key in ("active_release", "latest_installed_release", "active_is_latest", "known_releases"):
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
    run_root = Path(work_root) / "runs"
    if not run_root.exists():
        raise AgentTeamCliError("no AgentTeam runs found", run_root=str(run_root))
    run_dirs = [path for path in run_root.iterdir() if path.is_dir()]
    if not run_dirs:
        raise AgentTeamCliError("no AgentTeam runs found", run_root=str(run_root))
    return _canonical_run_dir(max(run_dirs, key=lambda path: path.stat().st_mtime))


def _build_project_status_summary(profile, authoring):
    return {
        "status_scope": "project",
        "project": profile.get("project_key") or "unknown",
        "status": "authoring",
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
    summary = {
        "project": profile.get("project_key") or "unknown",
        "latest_run": run_dir.name,
        "status": _status_run_state(snapshot, state),
        "liveness_status": liveness["liveness_status"],
        "runtime_release": liveness["runtime_release"],
        "processes": liveness["processes"],
        "tasks": task_counts,
        "integration": integration_counts,
        "integration_baseline": _paths_integration_baseline(run_dir, state),
        "inflight": _status_inflight_attempts(state),
        "workers": _status_worker_counts(worker_registry),
        "last_worker": _status_last_worker(worker_registry),
        "token_usage": token_usage_from_state(state),
        "manual_gates": manual_gate_count,
        "permission_requests": permission_request_count,
        "last_failure": _status_last_failure(snapshot, state),
        "authoring": _build_project_authoring_summary(profile),
        "run_dir": str(run_dir),
    }
    return summary


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
        "integration_baseline": _paths_integration_baseline(run_dir, state),
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
        "head_sha": baseline.get("integration_baseline_head_sha"),
    }


def _write_paths_text(summary):
    baseline = summary.get("integration_baseline") or {}
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
        f"integration_baseline_head: {baseline.get('head_sha') or 'unknown'}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _integrate_run_baseline(project_root, profile, run_dir):
    run_dir = Path(run_dir).resolve()
    run_status = _build_run_status_summary(profile, run_dir)
    if run_status.get("status") not in {"idle", "completed"}:
        raise AgentTeamCliError(
            "run is not ready to integrate",
            run_dir=str(run_dir),
            run_status=run_status.get("status") or "unknown",
        )
    dirty_status = _git_stdout(project_root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if dirty_status:
        raise AgentTeamCliError(
            "target repository must be clean before integrate",
            project_root=str(project_root),
            dirty_status=dirty_status,
        )

    state = _paths_run_state(run_dir)
    baseline = _paths_integration_baseline(run_dir, state)
    branch = baseline.get("branch")
    if not branch:
        raise AgentTeamCliError("integration baseline branch not found", run_dir=str(run_dir))
    branch_head = _git_stdout(project_root, ["rev-parse", "--verify", f"{branch}^{{commit}}"])
    current_head = _git_stdout(project_root, ["rev-parse", "HEAD"])
    if branch_head == current_head:
        return {
            "integrate_status": "up_to_date",
            "merge_status": "up_to_date",
            "project": profile.get("project_key") or "unknown",
            "taskpack_id": run_dir.name,
            "project_root": str(project_root),
            "run_dir": str(run_dir),
            "integration_baseline": {**baseline, "head_sha": branch_head},
            "before_head": current_head,
            "after_head": current_head,
        }
    ancestor = _git_completed(project_root, ["merge-base", "--is-ancestor", "HEAD", branch], check=False)
    if ancestor.returncode != 0:
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
    return {
        "integrate_status": "merged",
        "merge_status": "fast_forward",
        "project": profile.get("project_key") or "unknown",
        "taskpack_id": run_dir.name,
        "project_root": str(project_root),
        "run_dir": str(run_dir),
        "integration_baseline": {**baseline, "head_sha": branch_head},
        "before_head": current_head,
        "after_head": after_head,
        "merge_stdout": merge.stdout,
        "merge_stderr": merge.stderr,
    }


def _write_integrate_text(summary):
    baseline = summary.get("integration_baseline") or {}
    lines = [
        f"integrate_status: {summary['integrate_status']}",
        f"merge_status: {summary['merge_status']}",
        f"project: {summary['project']}",
        f"taskpack_id: {summary['taskpack_id']}",
        f"integration_baseline_branch: {baseline.get('branch') or 'none'}",
        f"before_head: {summary.get('before_head') or 'unknown'}",
        f"after_head: {summary.get('after_head') or 'unknown'}",
        f"run_dir: {summary['run_dir']}",
    ]
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
        f"status: {summary['status']}",
        f"liveness: {summary['liveness_status']}",
        (
            "tasks: "
            f"{summary['tasks']['done']} done, "
            f"{summary['tasks']['blocked']} blocked"
        ),
        f"integration: {summary['integration']['blocked']} blocked",
        f"integration_baseline_branch: {summary['integration_baseline'].get('branch') or 'none'}",
        f"integration_baseline_head: {summary['integration_baseline'].get('head_sha') or 'unknown'}",
        format_token_usage(summary.get("token_usage"), label="tokens"),
        f"inflight: {summary['inflight']['total']}",
        f"manual_gates: {summary['manual_gates']}",
        f"permission_requests: {summary['permission_requests']}",
    ]
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
    lines.append(f"run_dir: {summary['run_dir']}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _write_project_status_text(summary):
    authoring = summary.get("authoring") if isinstance(summary.get("authoring"), dict) else {}
    latest = authoring.get("latest") or {}
    lines = [
        f"project: {summary['project']}",
        f"status: {summary['status']}",
        f"authoring: {authoring.get('active_count', 0)} active, {authoring.get('total_count', 0)} recorded",
    ]
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
    artifact_snapshot = (
        result.get("artifact_snapshot")
        if isinstance(result.get("artifact_snapshot"), dict)
        else {}
    )
    lines = [
        f"status: {result.get('status') or result.get('continue_status') or 'unknown'}",
        f"taskpack_id: {result.get('taskpack_id') or 'unknown'}",
    ]
    if result.get("runtime"):
        lines.append(f"runtime: {result['runtime']}")
    if follow_up:
        lines.append(f"source_taskpack_id: {follow_up.get('source_taskpack_id') or 'unknown'}")
    if report:
        lines.append(
            "summary: "
            f"run_status={report.get('run_status') or 'unknown'} "
            f"tasks={report.get('task_count', 0)} "
            f"blocked={report.get('blocked_count', 0)}"
        )
        if isinstance(report.get("token_usage"), dict):
            lines.append(format_token_usage(report.get("token_usage"), label="tokens"))
        if not follow_up:
            completion_summary = (
                report.get("completion_summary")
                if isinstance(report.get("completion_summary"), dict)
                else {}
            )
            changed = _first_non_empty_text(completion_summary.get("what_changed"))
            if changed:
                lines.append(f"changed: {changed}")
            if completion_summary.get("integration"):
                lines.append(f"integration: {completion_summary['integration']}")
            if completion_summary.get("integration_recommendation"):
                lines.append(f"integration_recommendation: {completion_summary['integration_recommendation']}")
            next_step = _first_non_empty_text(completion_summary.get("next_steps"))
            if next_step:
                lines.append(f"next: {next_step}")
        if report.get("report_path"):
            lines.append(f"report: {report['report_path']}")
    if follow_up.get("source_report_path"):
        lines.append(f"source_report: {follow_up['source_report_path']}")
    if artifact_snapshot:
        lines.append(f"artifact_trace: {_artifact_snapshot_text(artifact_snapshot)}")
    run_dir = paths.get("run_dir")
    if run_dir:
        lines.append(f"run_dir: {run_dir}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


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


def _status_inflight_attempts(state):
    attempts = state.get("inflight_attempts") if isinstance(state, dict) else None
    if not isinstance(attempts, list):
        return {"total": 0, "tasks": []}
    tasks = [
        attempt.get("task_id")
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("task_id")
    ]
    return {"total": len(attempts), "tasks": tasks}


def _status_integration_counts(snapshot):
    queue = snapshot.get("integration_queue") if isinstance(snapshot, dict) else None
    if not isinstance(queue, dict):
        return {"total": 0, "blocked": 0, "verified": 0}
    statuses = [
        item.get("queue_status") or item.get("integration_queue_status")
        for item in queue.values()
        if isinstance(item, dict)
    ]
    return {
        "total": len([status for status in statuses if status]),
        "blocked": sum(1 for status in statuses if status == "blocked"),
        "verified": sum(1 for status in statuses if status == "verified"),
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
    worker = workers[-1]
    if not isinstance(worker, dict):
        return None
    worker_id = worker.get("worker_agent_id") or worker.get("worker_id") or "unknown-worker"
    worker_status = worker.get("worker_status") or "unknown"
    details = [f"{worker_id} {worker_status}"]
    if worker.get("exit_code") is not None:
        details.append(f"exit_code={worker['exit_code']}")
    if worker.get("stopped_by"):
        details.append(f"stopped_by={worker['stopped_by']}")
    return " ".join(details)


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
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
        notification_project=args.notification_project,
        feishu_enabled=bool(args.feishu_webhook_env),
        feishu_webhook_env=args.feishu_webhook_env,
        feishu_signing_secret_env=args.feishu_signing_secret_env,
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
        choices=["fake", "codex"],
        default=args.author_runtime or "codex",
    )
    runtime = _prompt_choice(
        "Runtime",
        choices=["auto", "fake", "codex"],
        default=args.runtime or "auto",
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
        one_shot=one_shot,
        max_inflight=args.max_inflight or 2,
        max_attempts=args.max_attempts or 1,
        commit_verified_integration=commit_verified_integration,
        notification_project=args.notification_project,
        feishu_enabled=feishu_enabled,
        feishu_webhook_env=feishu_webhook_env,
        feishu_signing_secret_env=feishu_signing_secret_env,
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
    )


def _override_or_profile(override, profile_value):
    return profile_value if override is None else override


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
        choices=["fake", "codex"],
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
):
    run_paths = _run_paths_for_frozen_taskpack(frozen_taskpack_dir, run_root)
    runtime_args = build_taskpack_runtime_args(
        frozen_taskpack_dir,
        run_root=run_paths["run_root"],
        daemon=not one_shot,
        max_inflight=max_inflight,
        max_attempts=max_attempts,
        commit_verified_integration=commit_verified_integration,
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
    runtime_root = str(Path(__file__).resolve().parents[1])
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

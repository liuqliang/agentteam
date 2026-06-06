import argparse
import json
import subprocess
import sys
from pathlib import Path

from .m0_runtime import answer_manual_gate, replay_events
from .taskpack import build_taskpack_runtime_args, freeze_taskpack, validate_taskpack
from .taskpack_author import draft_taskpack_from_goal


class AgentTeamCliError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentTeamCliError(message)


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
    _add_taskpack_draft_parser(taskpack_subcommands)
    _add_taskpack_validate_parser(taskpack_subcommands)
    _add_taskpack_freeze_parser(taskpack_subcommands)
    _add_run_parser(subcommands)
    _add_answer_parser(subcommands)
    _add_resume_parser(subcommands)
    return parser


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
    parser.set_defaults(handler=_handle_run)


def _add_notification_args(parser):
    parser.add_argument(
        "--notification-project",
        default="agentteam",
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


def _handle_taskpack_validate(args):
    return validate_taskpack(args.taskpack_dir)


def _handle_taskpack_freeze(args):
    return freeze_taskpack(args.taskpack_dir, args.frozen_root)


def _handle_submit(args):
    _complete_submit_args(args)
    work_root = Path(args.work_root).resolve()
    draft_root = work_root / "drafts"
    frozen_root = work_root / "frozen"
    run_root = work_root / "runs"
    runtime_backend = _submit_runtime_backend(args.runtime, args.author_runtime)

    draft = draft_taskpack_from_goal(
        project_root=args.project_root,
        goal=args.goal,
        draft_root=draft_root,
        author_runtime=args.author_runtime,
        taskpack_id=args.taskpack_id,
        codex_command=args.codex_command,
        codex_timeout_seconds=args.codex_timeout_seconds,
    )
    taskpack_dir = Path(draft["taskpack_dir"])
    _set_taskpack_runtime_backend(taskpack_dir, runtime_backend)
    validation = validate_taskpack(taskpack_dir)
    frozen = freeze_taskpack(taskpack_dir, frozen_root)
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
    return {
        "status": _submit_status_from_run(run),
        "taskpack_id": draft["taskpack_id"],
        "runtime": runtime_backend,
        "draft": draft,
        "validation": validation,
        "freeze": frozen,
        "run": run,
        "paths": {
            "work_root": str(work_root),
            "draft_root": str(draft_root),
            "frozen_root": str(frozen_root),
            "run_root": str(run_root),
        },
    }


def _handle_run(args):
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
    if completed.stdout:
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()
    return completed.returncode


def _handle_answer(args):
    return answer_manual_gate(
        args.run_dir,
        args.question_id,
        args.answer,
        operator=args.operator,
    )


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
):
    runtime_args = build_taskpack_runtime_args(
        frozen_taskpack_dir,
        run_root=run_root,
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
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


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

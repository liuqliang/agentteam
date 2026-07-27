"""Deterministic controller for the Phase 1 candidate-runtime live gate."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import agentteam as _gate_runtime
from . import profile as _profile_runtime
from . import projection_db as _projection_runtime
from . import release_manager as _release_runtime
from . import usage_live_smoke as _smoke_runtime


GATE_ID = "P1-LIVE"
MAX_PROVIDER_SPOOL_BYTES = 1024 * 1024
MAX_FAILURE_TEXT = 2000
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FIXED_ARTIFACT_RELATIVE = Path(
    "acceptance/model-invocation-live-smoke.v1.json"
)
FAILURE_ARTIFACT_RELATIVE = Path(
    "acceptance/failures/phase1-usage-acceptance-failure.v1.json"
)
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response_completed",
    "turn.completed",
    "turn_completed",
}
_USAGE_KEYS = ("usage", "token_usage", "total_token_usage")
_FIELD_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "prompt"),
    "cached_input_tokens": (
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_prompt_tokens",
    ),
    "output_tokens": ("output_tokens", "completion_tokens", "completion"),
    "reasoning_tokens": ("reasoning_tokens",),
    "total_tokens": ("total_tokens", "total"),
}


class Phase1UsageAcceptanceError(RuntimeError):
    """The controller refused or failed the live acceptance attempt."""


def run_acceptance(
    *,
    profile_project_root,
    candidate_project_root,
    implementation_run_id,
    implementation_run_dir=None,
    gate_epoch,
    acceptance_series_id,
    attempt_id,
    work_root,
    expected_commit,
    authorize_live_call,
    timeout_seconds=_smoke_runtime.DEFAULT_TIMEOUT_SECONDS,
    helper_runner=None,
    final_publisher=None,
):
    """Claim, execute, independently validate, and publish one live attempt."""
    if authorize_live_call is not True:
        raise Phase1UsageAcceptanceError(
            "explicit --authorize-live-call is required"
        )
    implementation_run_id = _safe_slug(
        implementation_run_id, "implementation_run_id"
    )
    acceptance_series_id = _safe_slug(
        acceptance_series_id, "acceptance_series_id"
    )
    attempt_id = _safe_slug(attempt_id, "attempt_id")
    run_id = _safe_slug(
        f"{acceptance_series_id}-{attempt_id}",
        "derived acceptance run_id",
        maximum=255,
    )
    if not isinstance(gate_epoch, int) or isinstance(gate_epoch, bool) or gate_epoch < 1:
        raise Phase1UsageAcceptanceError("gate_epoch must be a positive integer")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
        or timeout_seconds > 900
    ):
        raise Phase1UsageAcceptanceError(
            "timeout_seconds must be greater than zero and at most 900"
        )

    profile_project_root = Path(profile_project_root).resolve()
    candidate_project_root = Path(candidate_project_root).resolve()
    supplied_work_root = Path(work_root).resolve()
    profile = _profile_runtime.load_project_profile(profile_project_root)
    configured_work_root = Path(profile["work_root"]).resolve()
    if supplied_work_root != configured_work_root:
        raise Phase1UsageAcceptanceError(
            "supplied work_root differs from the explicit project profile"
        )
    work_root = configured_work_root
    _require_same_git_repository(profile_project_root, candidate_project_root)
    _verify_candidate_runtime_modules(candidate_project_root)
    implementation_run_dir = _resolve_implementation_run_dir(
        work_root,
        implementation_run_id,
        implementation_run_dir,
        expected_project_key=profile.get("project_key"),
    )
    context = _gate_runtime._require_post_backlog_gate_context(
        profile,
        implementation_run_dir,
    )
    _require_same_git_repository(
        context["project_root"],
        candidate_project_root,
    )
    declaration = _gate_runtime._require_gate_declaration(context, GATE_ID)
    if declaration.get("evidence_artifact") != FIXED_ARTIFACT_RELATIVE.as_posix():
        raise Phase1UsageAcceptanceError(
            "P1-LIVE declaration does not bind the fixed acceptance artifact"
        )

    initial_epoch = _gate_runtime._require_current_gate_epoch(
        context,
        gate_epoch,
    )
    initial_snapshot = _candidate_snapshot(
        candidate_project_root,
        expected_commit=expected_commit,
        require_clean=True,
    )
    _require_candidate_epoch_binding(
        context,
        initial_epoch,
        candidate_project_root,
        expected_commit,
    )
    prior_passed = _passed_receipt_result(
        context,
        initial_epoch,
        expected_commit=expected_commit,
    )
    if prior_passed is not None:
        return {**prior_passed, "idempotent": True}

    run_dir = work_root / "runs" / run_id
    existing = _existing_attempt_result(
        run_dir,
        expected_commit=expected_commit,
        implementation_run_id=implementation_run_id,
        gate_epoch=gate_epoch,
    )
    if existing is not None:
        return existing
    claim = _claim_attempt_run(
        work_root,
        run_id=run_id,
        project_key=profile["project_key"],
        taskpack_id=context["taskpack"].get("taskpack_id")
        or implementation_run_id,
        implementation_run_id=implementation_run_id,
        gate_epoch=gate_epoch,
        acceptance_series_id=acceptance_series_id,
        attempt_id=attempt_id,
        expected_commit=expected_commit,
        candidate_snapshot=initial_snapshot,
    )
    claimed_run_dir = Path(claim["run_dir"])
    if claimed_run_dir != run_dir:
        raise Phase1UsageAcceptanceError("claimed acceptance run path changed")

    try:
        with _gate_runtime._gate_mutation_locks(context, [GATE_ID]):
            current = _gate_runtime._require_current_gate_epoch(
                context,
                gate_epoch,
            )
            if current["digest"] != initial_epoch["digest"]:
                raise Phase1UsageAcceptanceError(
                    "gate epoch changed after acceptance claim"
                )
            locked_snapshot = _candidate_snapshot(
                candidate_project_root,
                expected_commit=expected_commit,
                require_clean=True,
            )
            if locked_snapshot != initial_snapshot:
                raise Phase1UsageAcceptanceError(
                    "candidate worktree changed before provider launch"
                )
            _require_candidate_epoch_binding(
                context,
                current,
                candidate_project_root,
                expected_commit,
            )
            _validate_claimed_attempt(
                run_dir,
                project_key=profile["project_key"],
                run_id=run_id,
                taskpack_id=context["taskpack"].get("taskpack_id")
                or implementation_run_id,
                implementation_run_id=implementation_run_id,
                gate_epoch=gate_epoch,
                acceptance_series_id=acceptance_series_id,
                attempt_id=attempt_id,
                expected_commit=expected_commit,
                candidate_snapshot=locked_snapshot,
            )
            open_invocations = _open_acceptance_invocations(
                work_root,
                implementation_run_id=implementation_run_id,
                gate_epoch=gate_epoch,
                exclude_run_id=run_id,
            )
            if open_invocations:
                raise Phase1UsageAcceptanceError(
                    "an earlier acceptance invocation remains open"
                )
            prior_passed = _passed_receipt_result(
                context,
                current,
                expected_commit=expected_commit,
            )
            if prior_passed is not None:
                _write_controller_result(
                    run_dir,
                    status="not_launched_prior_passed",
                    detail={"prior_run_id": prior_passed["run_id"]},
                )
                return {**prior_passed, "idempotent": True}

            receipt_path = _register_pending_receipt(
                context,
                current,
                declaration,
                evidence_run_id=run_id,
                expected_commit=expected_commit,
            )
            runtime_output_dir = run_dir / "acceptance" / "runtime-output"
            provisional_path = runtime_output_dir / "provisional.json"
            helper_request = {
                "profile": profile,
                "candidate_project_root": candidate_project_root,
                "candidate_runtime_root": _candidate_runtime_root(
                    candidate_project_root
                ),
                "run_id": run_id,
                "taskpack_id": context["taskpack"].get("taskpack_id")
                or implementation_run_id,
                "implementation_run_id": implementation_run_id,
                "gate_epoch": gate_epoch,
                "attempt_id": attempt_id,
                "output_dir": runtime_output_dir,
                "output": provisional_path,
                "expected_commit": expected_commit,
                "timeout_seconds": timeout_seconds,
            }
            if helper_runner is None:
                _run_helper_subprocess(helper_request)
            else:
                helper_runner(dict(helper_request))
            provisional = _read_json_object(
                provisional_path,
                "provisional helper result",
            )
            validated = _validate_provisional(
                provisional,
                helper_request=helper_request,
                profile=profile,
                acceptance_series_id=acceptance_series_id,
            )
            after_helper = _candidate_snapshot(
                candidate_project_root,
                expected_commit=expected_commit,
                require_clean=True,
            )
            if after_helper != locked_snapshot:
                raise Phase1UsageAcceptanceError(
                    "candidate HEAD or full status changed during live smoke"
                )
            staged_artifact = _build_artifact(
                validated,
                run_dir=run_dir,
                profile=profile,
                acceptance_series_id=acceptance_series_id,
                attempt_id=attempt_id,
                expected_commit=expected_commit,
                controller_status="staged",
            )
            staged_path = (
                run_dir
                / "acceptance"
                / "staging"
                / "model-invocation-live-smoke.staged.json"
            )
            _atomic_write_json(staged_path, staged_artifact, replace=False)
            projection = _validate_isolated_projection(
                work_root,
                staged_path,
                run_dir=run_dir,
                run_id=run_id,
                expected_totals=validated["provider_totals"],
            )

            final_epoch = _gate_runtime._require_current_gate_epoch(
                context,
                gate_epoch,
            )
            if final_epoch["digest"] != current["digest"]:
                raise Phase1UsageAcceptanceError(
                    "gate epoch changed before acceptance publication"
                )
            final_snapshot = _candidate_snapshot(
                candidate_project_root,
                expected_commit=expected_commit,
                require_clean=True,
            )
            if final_snapshot != locked_snapshot:
                raise Phase1UsageAcceptanceError(
                    "candidate HEAD or full status changed before publication"
                )
            _require_candidate_epoch_binding(
                context,
                final_epoch,
                candidate_project_root,
                expected_commit,
            )
            final_artifact = {
                **staged_artifact,
                "controller_validation_status": "passed",
            }
            _validate_live_artifact_schema(final_artifact)
            final_path = run_dir / FIXED_ARTIFACT_RELATIVE
            publisher = final_publisher or _publish_final_artifact
            publisher(final_path, final_artifact)
            gate_decision = _validate_p1_live_receipt(
                context,
                final_epoch,
                declaration,
                receipt_path=receipt_path,
                artifact_path=final_path,
                expected_commit=expected_commit,
            )
            _write_controller_result(
                run_dir,
                status="completed",
                detail={
                    "artifact": FIXED_ARTIFACT_RELATIVE.as_posix(),
                    "receipt": str(receipt_path),
                },
            )
            if gate_decision.get("state") != "passed":
                raise Phase1UsageAcceptanceError(
                    "published P1-LIVE artifact did not resolve its gate receipt"
                )
            return {
                "status": "passed",
                "run_id": run_id,
                "implementation_run_id": implementation_run_id,
                "gate_epoch": gate_epoch,
                "candidate_commit_sha": expected_commit,
                "artifact_path": str(final_path),
                "receipt_path": str(receipt_path),
                "provider_totals": validated["provider_totals"],
                "projection": projection,
                "idempotent": False,
            }
    except Exception as exc:
        _write_failure_artifact(
            run_dir,
            implementation_run_id=implementation_run_id,
            gate_epoch=gate_epoch,
            acceptance_series_id=acceptance_series_id,
            attempt_id=attempt_id,
            error=exc,
        )
        _write_controller_result(
            run_dir,
            status="failed",
            detail={
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:MAX_FAILURE_TEXT],
            },
        )
        if isinstance(exc, Phase1UsageAcceptanceError):
            raise
        raise Phase1UsageAcceptanceError(str(exc)) from exc


def decode_bounded_provider_spool(path, *, maximum_bytes=MAX_PROVIDER_SPOOL_BYTES):
    """Decode the frozen terminal event contract without token_usage.py."""
    path = Path(path)
    size = path.stat().st_size
    if size > maximum_bytes:
        raise Phase1UsageAcceptanceError(
            f"provider spool exceeds {maximum_bytes} bytes"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Phase1UsageAcceptanceError(
            "provider spool is not valid UTF-8"
        ) from exc
    terminal = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise Phase1UsageAcceptanceError(
                f"malformed provider JSONL at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise Phase1UsageAcceptanceError(
                f"provider event at line {line_number} is not an object"
            )
        if event.get("type") in _TERMINAL_EVENT_TYPES:
            terminal = event
    if terminal is None:
        raise Phase1UsageAcceptanceError(
            "provider spool has no frozen terminal event"
        )
    usage = next(
        (
            terminal.get(key)
            for key in _USAGE_KEYS
            if isinstance(terminal.get(key), dict)
        ),
        None,
    )
    if usage is None:
        raise Phase1UsageAcceptanceError(
            "terminal provider event has no direct usage object"
        )
    totals = {
        field: _first_nonnegative_integer(
            usage,
            _FIELD_ALIASES[field],
            required=field in {"input_tokens", "output_tokens", "total_tokens"},
        )
        for field in _TOKEN_FIELDS
    }
    return {
        "terminal_event": terminal,
        "terminal_event_sha256": _sha256_json(terminal),
        "provider_totals": totals,
    }


def _validate_provisional(
    provisional,
    *,
    helper_request,
    profile,
    acceptance_series_id,
):
    expected = {
        "project": profile["project_key"],
        "run_id": helper_request["run_id"],
        "taskpack_id": helper_request["taskpack_id"],
        "implementation_run_id": helper_request["implementation_run_id"],
        "gate_epoch": helper_request["gate_epoch"],
        "acceptance_attempt_id": helper_request["attempt_id"],
        "candidate_commit_sha": helper_request["expected_commit"],
        "terminal_status": "completed",
    }
    mismatches = [
        key for key, value in expected.items() if provisional.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageAcceptanceError(
            "provisional helper binding mismatch: " + ", ".join(mismatches)
        )
    candidate_runtime_root = Path(
        str(provisional.get("candidate_runtime_root") or "")
    ).resolve()
    expected_runtime_root = Path(helper_request["candidate_runtime_root"]).resolve()
    if candidate_runtime_root != expected_runtime_root:
        raise Phase1UsageAcceptanceError(
            "helper candidate_runtime_root does not match candidate runtime"
        )
    spool_relative = Path(str(provisional.get("bounded_raw_spool_path") or ""))
    if spool_relative.is_absolute() or ".." in spool_relative.parts:
        raise Phase1UsageAcceptanceError(
            "helper bounded_raw_spool_path is unsafe"
        )
    spool_path = (Path(helper_request["output_dir"]) / spool_relative).resolve()
    _require_within(
        spool_path,
        helper_request["output_dir"],
        "bounded provider spool",
    )
    decoded = decode_bounded_provider_spool(spool_path)
    if (
        provisional.get("provider_terminal_snapshot_sha256")
        != decoded["terminal_event_sha256"]
    ):
        raise Phase1UsageAcceptanceError(
            "helper provider terminal event hash mismatch"
        )
    if provisional.get("provider_totals") != decoded["provider_totals"]:
        raise Phase1UsageAcceptanceError(
            "helper provider totals do not match independent decoder"
        )
    start = provisional.get("invocation_start_record")
    terminal = provisional.get("invocation_usage_record")
    _validate_lifecycle_pair(
        start,
        terminal,
        expected={
            "project": profile["project_key"],
            "run_id": helper_request["run_id"],
            "taskpack_id": helper_request["taskpack_id"],
            "implementation_run_id": helper_request["implementation_run_id"],
            "gate_epoch": helper_request["gate_epoch"],
            "attempt_id": helper_request["attempt_id"],
            "usage_stage": "acceptance_live_smoke",
            "coverage_class": "supported_model_invocation",
        },
    )
    if terminal.get("usage_status") != "reported":
        raise Phase1UsageAcceptanceError(
            "acceptance terminal usage is not provider-reported"
        )
    normalized = {field: terminal.get(field) for field in _TOKEN_FIELDS}
    if normalized != decoded["provider_totals"]:
        raise Phase1UsageAcceptanceError(
            "terminal lifecycle totals do not match independent provider totals"
        )
    return {
        "start": start,
        "terminal": terminal,
        "provider_totals": decoded["provider_totals"],
        "provider_terminal_snapshot_sha256": decoded[
            "terminal_event_sha256"
        ],
        "spool_path": spool_path,
        "candidate_runtime_root": candidate_runtime_root,
        "acceptance_series_id": acceptance_series_id,
    }


def _validate_lifecycle_pair(start, terminal, *, expected):
    if not isinstance(start, dict) or not isinstance(terminal, dict):
        raise Phase1UsageAcceptanceError(
            "helper result requires one start and one terminal record"
        )
    if start.get("start_schema_version") != "model_invocation_started.v1":
        raise Phase1UsageAcceptanceError("invalid invocation start schema")
    if terminal.get("usage_schema_version") != "model_invocation_usage.v1":
        raise Phase1UsageAcceptanceError("invalid invocation usage schema")
    invocation_id = start.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id.startswith("INV-"):
        raise Phase1UsageAcceptanceError("invalid invocation ID")
    if terminal.get("invocation_id") != invocation_id:
        raise Phase1UsageAcceptanceError(
            "start and terminal invocation IDs differ"
        )
    for key, value in expected.items():
        if start.get(key) != value or terminal.get(key) != value:
            raise Phase1UsageAcceptanceError(
                f"lifecycle correlation differs for {key}"
            )
    if terminal.get("terminal_status") != "completed":
        raise Phase1UsageAcceptanceError(
            "acceptance lifecycle did not complete"
        )
    expected_terminal_fields = {
        "terminal_writer": "acceptance_controller",
        "usage_status": "reported",
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": "provider_reported",
        "provider_usage_snapshot": None,
        "unavailable_reason": None,
    }
    if any(
        terminal.get(key) != value
        for key, value in expected_terminal_fields.items()
    ):
        raise Phase1UsageAcceptanceError(
            "acceptance terminal accounting contract is invalid"
        )
    if (
        start.get("provider_resume_mode") != "new"
        or start.get("requested_provider_session_id") is not None
        or start.get("provider_predecessor_invocation_id") is not None
    ):
        raise Phase1UsageAcceptanceError(
            "acceptance invocation must be a new provider execution"
        )


def _build_artifact(
    validated,
    *,
    run_dir,
    profile,
    acceptance_series_id,
    attempt_id,
    expected_commit,
    controller_status,
):
    spool_relative = validated["spool_path"].relative_to(run_dir).as_posix()
    return {
        "schema_version": "model_invocation_live_smoke.v1",
        "project": profile["project_key"],
        "run_id": validated["start"]["run_id"],
        "run_kind": "acceptance_evidence",
        "taskpack_id": validated["start"]["taskpack_id"],
        "implementation_run_id": validated["start"][
            "implementation_run_id"
        ],
        "gate_epoch": validated["start"]["gate_epoch"],
        "acceptance_series_id": acceptance_series_id,
        "acceptance_attempt_id": attempt_id,
        "git_object_format": _git_object_format(
            Path(validated["candidate_runtime_root"]).parents[2]
        ),
        "candidate_commit_sha": expected_commit,
        "validated_code_sha": expected_commit,
        "candidate_runtime_root": str(validated["candidate_runtime_root"]),
        "terminal_status": "completed",
        "invocation_start_record": validated["start"],
        "invocation_usage_record": validated["terminal"],
        "provider_terminal_snapshot_sha256": validated[
            "provider_terminal_snapshot_sha256"
        ],
        "provider_totals": validated["provider_totals"],
        "reconciliation_status": "matched",
        "controller_validation_status": controller_status,
        "started_at": validated["start"]["started_at"],
        "finished_at": validated["terminal"]["finished_at"],
        "bounded_raw_spool_path": spool_relative,
    }


def _validate_isolated_projection(
    work_root,
    staged_path,
    *,
    run_dir,
    run_id,
    expected_totals,
):
    isolated_root = run_dir / "acceptance" / "isolated-projection"
    first = _projection_runtime.build_isolated_acceptance_projection(
        work_root,
        staged_path,
        isolated_root / "first.db",
        filters={"run": run_id},
    )
    second = _projection_runtime.build_isolated_acceptance_projection(
        work_root,
        staged_path,
        isolated_root / "replay.db",
        filters={"run": run_id},
    )
    first_usage = first["model_invocation_usage"]
    second_usage = second["model_invocation_usage"]
    stable_keys = (
        "invocation_count",
        "terminal_invocation_count",
        "supported_invocation_count",
        "open_invocations",
        "lifecycle_terminal_coverage",
        "token_usage_coverage",
        "reported_token_totals",
        "filtered_invocation_digest",
    )
    if any(first_usage.get(key) != second_usage.get(key) for key in stable_keys):
        raise Phase1UsageAcceptanceError(
            "isolated acceptance projection is not replay-stable"
        )
    if (
        first_usage.get("invocation_count") != 1
        or first_usage.get("terminal_invocation_count") != 1
        or first_usage.get("supported_invocation_count") != 1
        or first_usage.get("open_invocations") != 0
    ):
        raise Phase1UsageAcceptanceError(
            "isolated acceptance projection did not produce one closed invocation"
        )
    for key in ("lifecycle_terminal_coverage", "token_usage_coverage"):
        coverage = first_usage.get(key) or {}
        if coverage.get("covered") != 1 or coverage.get("total") != 1:
            raise Phase1UsageAcceptanceError(
                f"isolated projection {key} is not 1/1"
            )
    projected_totals = first_usage.get("reported_token_totals") or {}
    if any(projected_totals.get(key) != value for key, value in expected_totals.items()):
        raise Phase1UsageAcceptanceError(
            "isolated projection totals do not match provider totals"
        )
    return {
        "status": "passed",
        "invocation_count": 1,
        "lifecycle_terminal_coverage": "1/1",
        "token_usage_coverage": "1/1",
        "replay_stable": True,
        "filtered_invocation_digest": first_usage.get(
            "filtered_invocation_digest"
        ),
    }


def _run_helper_subprocess(request):
    command = [
        sys.executable,
        "-m",
        "agentteam_runtime.usage_live_smoke",
        "--project-root",
        str(request["candidate_project_root"]),
        "--project",
        request["profile"]["project_key"],
        "--run-id",
        request["run_id"],
        "--taskpack-id",
        request["taskpack_id"],
        "--implementation-run-id",
        request["implementation_run_id"],
        "--gate-epoch",
        str(request["gate_epoch"]),
        "--attempt-id",
        request["attempt_id"],
        "--output-dir",
        str(request["output_dir"]),
        "--output",
        str(request["output"]),
        "--expected-commit",
        request["expected_commit"],
        "--timeout-seconds",
        str(request["timeout_seconds"]),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(request["candidate_runtime_root"])
    completed = subprocess.run(
        command,
        cwd=request["candidate_project_root"],
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=request["timeout_seconds"] + 30,
    )
    if completed.returncode != 0:
        raise Phase1UsageAcceptanceError(
            "bounded helper failed: " + completed.stderr[-MAX_FAILURE_TEXT:]
        )


def _claim_attempt_run(
    work_root,
    *,
    run_id,
    project_key,
    taskpack_id,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    attempt_id,
    expected_commit,
    candidate_snapshot,
):
    runs_root = work_root / "runs"
    staging_root = work_root / "run-staging"
    runs_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    if os.stat(runs_root).st_dev != os.stat(staging_root).st_dev:
        raise Phase1UsageAcceptanceError(
            "run-staging and runs must share one filesystem"
        )
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    owner_token = f"ACCEPTANCE-CONTROLLER-{uuid.uuid4().hex}"
    created_at = _utc_now()
    claim_record = {
        "schema_version": "phase1_usage_controller_claim.v1",
        "project_key": project_key,
        "run_id": run_id,
        "run_kind": "acceptance_evidence",
        "taskpack_id": taskpack_id,
        "implementation_run_id": implementation_run_id,
        "gate_id": GATE_ID,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "acceptance_attempt_id": attempt_id,
        "expected_commit": expected_commit,
        "candidate_head": candidate_snapshot["head"],
        "candidate_ordinary_status_sha256": candidate_snapshot[
            "ordinary_status_sha256"
        ],
        "candidate_full_status_sha256": candidate_snapshot[
            "full_status_sha256"
        ],
        "owner_token": owner_token,
        "controller_pid": os.getpid(),
        "controller_boot_id": _boot_id(),
        "controller_start_ticks": _process_start_ticks(),
        "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        "claim_status": "claimed",
        "claimed_at": created_at,
    }
    identity = {
        "schema_version": "run_identity.v1",
        "project_key": project_key,
        "run_id": run_id,
        "taskpack_id": taskpack_id,
        "run_kind": "acceptance_evidence",
        "created_at": created_at,
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
    }
    staging = staging_root / f"{run_id}.{os.getpid()}.{uuid.uuid4().hex}"
    target = runs_root / run_id
    try:
        (staging / "state").mkdir(parents=True, exist_ok=False)
        _atomic_write_json(
            staging / "state" / "controller_claim.v1.json",
            claim_record,
            replace=False,
        )
        _atomic_write_json(
            staging / "state" / "run_identity.v1.json",
            identity,
            replace=False,
        )
        _fsync_directory(staging / "state")
        _fsync_directory(staging)
        _gate_runtime._rename_directory_noreplace(staging, target)
        _fsync_directory(runs_root)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        if target.exists():
            raise Phase1UsageAcceptanceError(
                "acceptance run ID is already claimed"
            ) from exc
        raise
    _release_runtime.validate_acceptance_run_identity(
        target,
        expected_project_key=project_key,
        expected_implementation_run_id=implementation_run_id,
        expected_gate_epoch=gate_epoch,
    )
    return {"run_dir": str(target), "claim": claim_record, "identity": identity}


def _validate_claimed_attempt(
    run_dir,
    *,
    project_key,
    run_id,
    taskpack_id,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    attempt_id,
    expected_commit,
    candidate_snapshot,
):
    claim = _read_json_object(
        run_dir / "state" / "controller_claim.v1.json",
        "acceptance controller claim",
    )
    identity = _read_json_object(
        run_dir / "state" / "run_identity.v1.json",
        "acceptance run identity",
    )
    expected_claim = {
        "project_key": project_key,
        "run_id": run_id,
        "run_kind": "acceptance_evidence",
        "taskpack_id": taskpack_id,
        "implementation_run_id": implementation_run_id,
        "gate_id": GATE_ID,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "acceptance_attempt_id": attempt_id,
        "expected_commit": expected_commit,
        "candidate_head": candidate_snapshot["head"],
        "candidate_ordinary_status_sha256": candidate_snapshot[
            "ordinary_status_sha256"
        ],
        "candidate_full_status_sha256": candidate_snapshot[
            "full_status_sha256"
        ],
        "claim_status": "claimed",
    }
    mismatches = [
        key for key, value in expected_claim.items() if claim.get(key) != value
    ]
    expected_identity = {
        "project_key": project_key,
        "run_id": run_id,
        "taskpack_id": taskpack_id,
        "run_kind": "acceptance_evidence",
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
    }
    mismatches.extend(
        f"run_identity.{key}"
        for key, value in expected_identity.items()
        if identity.get(key) != value
    )
    if mismatches:
        raise Phase1UsageAcceptanceError(
            "acceptance claim changed under lock: " + ", ".join(mismatches)
        )


def _register_pending_receipt(
    context,
    current,
    declaration,
    *,
    evidence_run_id,
    expected_commit,
):
    if current["record"]["integration_head_sha"] != expected_commit:
        raise Phase1UsageAcceptanceError(
            "gate epoch integration head differs from expected commit"
        )
    receipt_path = _gate_runtime._gate_receipt_path(
        context,
        current["record"],
        GATE_ID,
    )
    previous = _read_json_if_exists(receipt_path)
    attempts = list(previous.get("attempt_history", [])) if previous else []
    if previous:
        attempts.append(
            {
                "evidence_run_id": previous.get("evidence_run_id"),
                "evidence_run_relative_path": previous.get(
                    "evidence_run_relative_path"
                ),
                "registered_at": previous.get("registered_at"),
                "superseded_at": _utc_now(),
            }
        )
    receipt = {
        "schema_version": "post_backlog_gate_receipt.v1",
        "implementation_run_id": context["run_dir"].name,
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "gate_id": GATE_ID,
        "evidence_run_id": evidence_run_id,
        "evidence_run_relative_path": f"runs/{evidence_run_id}",
        "expected_integration_head_sha": expected_commit,
        "git_object_format": current["record"]["git_object_format"],
        "evidence_artifact": declaration["evidence_artifact"],
        "evidence_schema": declaration["evidence_schema"],
        "attempt_history": attempts[-20:],
        "registered_at": _utc_now(),
    }
    _gate_runtime._validate_gate_record_schema(
        "post_backlog_gate_receipt.schema.json",
        receipt,
    )
    _gate_runtime._atomic_write_json(receipt_path, receipt)
    return receipt_path


def _validate_p1_live_receipt(
    context,
    current,
    declaration,
    *,
    receipt_path,
    artifact_path,
    expected_commit,
):
    """Resolve P1-LIVE without the production parser or remote schema lookup."""
    receipt = _read_json_object(receipt_path, "P1-LIVE receipt")
    _gate_runtime._validate_gate_record_schema(
        "post_backlog_gate_receipt.schema.json",
        receipt,
    )
    expected_receipt = {
        "implementation_run_id": context["run_dir"].name,
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "gate_id": GATE_ID,
        "evidence_run_id": Path(artifact_path).parents[1].name,
        "evidence_run_relative_path": (
            f"runs/{Path(artifact_path).parents[1].name}"
        ),
        "expected_integration_head_sha": expected_commit,
        "git_object_format": current["record"]["git_object_format"],
        "evidence_artifact": declaration["evidence_artifact"],
        "evidence_schema": declaration["evidence_schema"],
    }
    mismatches = [
        key
        for key, value in expected_receipt.items()
        if receipt.get(key) != value
    ]
    expected_artifact = (
        context["work_root"]
        / receipt["evidence_run_relative_path"]
        / declaration["evidence_artifact"]
    ).resolve()
    if expected_artifact != Path(artifact_path).resolve():
        mismatches.append("evidence_run_relative_path")
    if mismatches:
        raise Phase1UsageAcceptanceError(
            "P1-LIVE receipt binding mismatch: " + ", ".join(mismatches)
        )
    artifact = _read_json_object(artifact_path, "fixed P1-LIVE artifact")
    _validate_live_artifact_schema(artifact)
    if (
        artifact.get(declaration["required_status_field"])
        != declaration["required_status_value"]
    ):
        raise Phase1UsageAcceptanceError(
            "fixed P1-LIVE artifact does not carry the required passed status"
        )
    commit_field = declaration.get("commit_field")
    if commit_field and artifact.get(commit_field) != expected_commit:
        raise Phase1UsageAcceptanceError(
            f"fixed P1-LIVE artifact {commit_field} differs from expected commit"
        )
    return {
        "state": "passed",
        "gate_id": GATE_ID,
        "evidence_sha256": hashlib.sha256(
            Path(artifact_path).read_bytes()
        ).hexdigest(),
    }


def _passed_receipt_result(context, current, *, expected_commit):
    declaration = context["declarations_by_id"][GATE_ID]
    receipt_path = _gate_runtime._gate_receipt_path(
        context,
        current["record"],
        GATE_ID,
    )
    receipt = _read_json_if_exists(receipt_path)
    if not receipt:
        return None
    run_id = receipt.get("evidence_run_id")
    if not isinstance(run_id, str):
        return None
    artifact_path = (
        context["work_root"]
        / "runs"
        / run_id
        / declaration["evidence_artifact"]
    )
    artifact = _read_json_if_exists(artifact_path)
    if (
        artifact.get("controller_validation_status") != "passed"
        or artifact.get("validated_code_sha") != expected_commit
    ):
        return None
    _validate_p1_live_receipt(
        context,
        current,
        declaration,
        receipt_path=receipt_path,
        artifact_path=artifact_path,
        expected_commit=expected_commit,
    )
    return {
        "status": "passed",
        "run_id": run_id,
        "implementation_run_id": context["run_dir"].name,
        "gate_epoch": current["record"]["epoch_number"],
        "candidate_commit_sha": expected_commit,
        "artifact_path": str(artifact_path),
        "receipt_path": str(receipt_path),
        "provider_totals": artifact["provider_totals"],
    }


def _existing_attempt_result(
    run_dir,
    *,
    expected_commit,
    implementation_run_id,
    gate_epoch,
):
    if not run_dir.exists() and not run_dir.is_symlink():
        return None
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise Phase1UsageAcceptanceError(
            "acceptance run path exists but is unsafe"
        )
    artifact_path = run_dir / FIXED_ARTIFACT_RELATIVE
    artifact = _read_json_if_exists(artifact_path)
    if artifact:
        _validate_live_artifact_schema(artifact)
        raise Phase1UsageAcceptanceError(
            "existing acceptance artifact has no current passed receipt; "
            "the run is recovery-only"
        )
    controller_result = _read_json_if_exists(
        run_dir / "state" / "controller_result.v1.json"
    )
    if controller_result.get("status") in {
        "failed",
        "completed",
        "not_launched_prior_passed",
    }:
        raise Phase1UsageAcceptanceError(
            "existing acceptance run is recovery-only; use a fresh attempt ID"
        )
    claim = _read_json_if_exists(
        run_dir / "state" / "controller_claim.v1.json"
    )
    if _controller_claim_is_live(claim):
        return {
            "status": "in_progress",
            "run_id": run_dir.name,
            "implementation_run_id": implementation_run_id,
            "gate_epoch": gate_epoch,
            "candidate_commit_sha": expected_commit,
            "idempotent": True,
        }
    raise Phase1UsageAcceptanceError(
        "existing acceptance run is recovery-only; use a fresh attempt ID"
    )


def _controller_claim_is_live(claim):
    if (
        not isinstance(claim, dict)
        or claim.get("claim_status") != "claimed"
        or claim.get("controller_boot_id") != _boot_id()
    ):
        return False
    pid = claim.get("controller_pid")
    expected_ticks = claim.get("controller_start_ticks")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(expected_ticks, int)
        or isinstance(expected_ticks, bool)
        or expected_ticks < 0
    ):
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        actual_ticks = int(stat[stat.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return False
    return actual_ticks == expected_ticks


def _open_acceptance_invocations(
    work_root,
    *,
    implementation_run_id,
    gate_epoch,
    exclude_run_id,
):
    findings = []
    runs_root = work_root / "runs"
    if not runs_root.is_dir():
        return findings
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        if run_dir.name == exclude_run_id:
            continue
        identity = _read_json_if_exists(
            run_dir / "state" / "run_identity.v1.json"
        )
        if (
            identity.get("run_kind") != "acceptance_evidence"
            or identity.get("implementation_run_id") != implementation_run_id
            or identity.get("gate_epoch") != gate_epoch
        ):
            continue
        for started in run_dir.rglob("model_invocations/*/started.json"):
            terminal = started.with_name("terminal.json")
            if not terminal.is_file():
                findings.append(str(started))
    return findings[:20]


def _candidate_snapshot(candidate_root, *, expected_commit, require_clean):
    head = _git_stdout(candidate_root, ["rev-parse", "HEAD"])
    object_format = _git_object_format(candidate_root)
    expected_length = 40 if object_format == "sha1" else 64
    if (
        len(expected_commit) != expected_length
        or any(character not in "0123456789abcdef" for character in expected_commit)
    ):
        raise Phase1UsageAcceptanceError(
            f"expected_commit is not a canonical {object_format} Git OID"
        )
    if head != expected_commit:
        raise Phase1UsageAcceptanceError(
            "candidate HEAD differs from expected_commit"
        )
    ordinary = _git_bytes(
        candidate_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if require_clean and ordinary:
        raise Phase1UsageAcceptanceError(
            "candidate worktree has tracked or untracked changes"
        )
    full = _git_bytes(
        candidate_root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
        ],
    )
    return {
        "head": head,
        "ordinary_status_sha256": hashlib.sha256(ordinary).hexdigest(),
        "full_status_sha256": hashlib.sha256(full).hexdigest(),
    }


def _require_candidate_epoch_binding(
    context,
    current,
    candidate_project_root,
    expected_commit,
):
    epoch = current["record"]
    resolved_head = _gate_runtime._resolved_epoch_integration_head(
        context["project_root"],
        epoch,
    )
    if resolved_head != expected_commit or epoch["integration_head_sha"] != expected_commit:
        raise Phase1UsageAcceptanceError(
            "candidate commit does not match the gate epoch integration baseline"
        )
    integration_worktree = _gate_runtime._gate_integration_worktree(
        context,
        epoch,
    ).resolve()
    if integration_worktree != candidate_project_root.resolve():
        raise Phase1UsageAcceptanceError(
            "candidate root is not the gate epoch integration worktree"
        )


def _verify_candidate_runtime_modules(candidate_project_root):
    runtime_root = _candidate_runtime_root(candidate_project_root)
    package_root = runtime_root / "agentteam_runtime"
    modules = tuple(
        module
        for name, module in sys.modules.items()
        if (
            name == "agentteam_runtime"
            or name.startswith("agentteam_runtime.")
        )
        and getattr(module, "__file__", None)
    )
    invalid = []
    for module in modules:
        module_path = Path(str(getattr(module, "__file__", ""))).resolve()
        try:
            module_path.relative_to(package_root)
        except ValueError:
            invalid.append(f"{module.__name__}:{module_path}")
    if invalid:
        raise Phase1UsageAcceptanceError(
            "Phase 1 runtime module did not resolve from candidate: "
            + ", ".join(invalid)
        )


def _candidate_runtime_root(candidate_project_root):
    runtime_root = (
        Path(candidate_project_root)
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    ).resolve()
    if not (runtime_root / "agentteam_runtime" / "__init__.py").is_file():
        raise Phase1UsageAcceptanceError(
            f"candidate runtime root is missing: {runtime_root}"
        )
    return runtime_root


def _require_same_git_repository(left, right):
    left_common = _git_common_directory(left)
    right_common = _git_common_directory(right)
    if left_common != right_common:
        raise Phase1UsageAcceptanceError(
            "profile and candidate roots do not share one Git common directory"
        )


def _git_common_directory(project_root):
    raw = _git_stdout(project_root, ["rev-parse", "--git-common-dir"])
    path = Path(raw)
    if not path.is_absolute():
        path = Path(project_root) / path
    return path.resolve()


def _validate_live_artifact_schema(artifact):
    schema_root = Path(__file__).resolve().parents[2] / "schemas"
    schema_path = schema_root / "model_invocation_live_smoke.schema.json"
    try:
        import jsonschema

        schemas = {}
        for path in schema_root.glob("*.schema.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("$id"), str):
                schemas[value["$id"]] = value
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        resolver = jsonschema.RefResolver.from_schema(
            schema,
            store=schemas,
        )
        errors = sorted(
            validator_class(
                schema,
                resolver=resolver,
                format_checker=jsonschema.FormatChecker(),
            ).iter_errors(artifact),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except Exception as exc:
        if exc.__class__.__module__.startswith(
            ("jsonschema", "referencing")
        ):
            raise Phase1UsageAcceptanceError(
                f"live artifact schema validation failed: {exc}"
            ) from exc
        raise
    if errors:
        detail = "; ".join(
            (
                ".".join(str(part) for part in error.absolute_path)
                or "<root>"
            )
            + ": "
            + error.message
            for error in errors[:10]
        )
        raise Phase1UsageAcceptanceError(
            f"live artifact schema validation failed: {detail}"
        )


def _publish_final_artifact(path, artifact):
    _atomic_write_json(path, artifact, replace=False)


def _atomic_write_json(path, value, *, replace):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
                raise Phase1UsageAcceptanceError(
                    f"immutable artifact already exists: {path}"
                ) from exc
            temporary.unlink()
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_failure_artifact(
    run_dir,
    *,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    attempt_id,
    error,
):
    value = {
        "schema_version": "phase1_usage_acceptance_failure.v1",
        "status": "failed",
        "run_id": run_dir.name,
        "implementation_run_id": implementation_run_id,
        "gate_id": GATE_ID,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "acceptance_attempt_id": attempt_id,
        "error_type": error.__class__.__name__,
        "error": str(error)[:MAX_FAILURE_TEXT],
        "failed_at": _utc_now(),
    }
    path = run_dir / FAILURE_ARTIFACT_RELATIVE
    if not path.exists():
        _atomic_write_json(path, value, replace=False)


def _write_controller_result(run_dir, *, status, detail):
    value = {
        "schema_version": "phase1_usage_controller_result.v1",
        "status": status,
        "finished_at": _utc_now(),
        **detail,
    }
    path = run_dir / "state" / "controller_result.v1.json"
    if not path.exists():
        _atomic_write_json(path, value, replace=False)


def _read_json_object(path, label):
    value = _read_json_if_exists(path)
    if not value:
        raise Phase1UsageAcceptanceError(f"{label} is missing or invalid")
    return value


def _read_json_if_exists(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_nonnegative_integer(source, aliases, *, required):
    present = [source[key] for key in aliases if key in source]
    if len({json.dumps(value, sort_keys=True) for value in present}) > 1:
        raise Phase1UsageAcceptanceError(
            f"terminal provider usage aliases conflict for {aliases[0]}"
        )
    value = present[0] if present else None
    if value is None and not required:
        return None
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        label = aliases[0]
        raise Phase1UsageAcceptanceError(
            f"terminal provider usage {label} is missing or invalid"
        )
    return value


def _sha256_json(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_object_format(project_root):
    value = _git_stdout(project_root, ["rev-parse", "--show-object-format"])
    if value not in {"sha1", "sha256"}:
        raise Phase1UsageAcceptanceError(
            f"unsupported Git object format: {value}"
        )
    return value


def _git_stdout(project_root, arguments):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise Phase1UsageAcceptanceError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _git_bytes(project_root, arguments):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise Phase1UsageAcceptanceError(
            f"git {' '.join(arguments)} failed"
        )
    return completed.stdout


def _require_within(path, root, label):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise Phase1UsageAcceptanceError(
            f"{label} escapes its declared root"
        ) from exc


def _safe_slug(value, label, *, maximum=128):
    if not isinstance(value, str) or len(value) > maximum or not SAFE_SLUG.fullmatch(value):
        raise Phase1UsageAcceptanceError(f"{label} must be a bounded safe slug")
    return value


def _resolve_implementation_run_dir(
    work_root,
    implementation_run_id,
    supplied_run_dir=None,
    *,
    expected_project_key=None,
):
    """Resolve one explicit direct or vN-namespaced implementation run."""
    work_root = Path(work_root).resolve()
    runs_root = work_root / "runs"
    requested = (
        runs_root / implementation_run_id
        if supplied_run_dir is None
        else Path(supplied_run_dir).expanduser()
    )
    if requested.is_symlink():
        raise Phase1UsageAcceptanceError(
            "implementation_run_dir must not be a symlink"
        )
    run_dir = requested.resolve()
    try:
        relative = run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise Phase1UsageAcceptanceError(
            "implementation_run_dir must be below the configured runs root"
        ) from exc
    if len(relative.parts) == 1:
        pass
    elif (
        len(relative.parts) == 2
        and _release_runtime.RUN_NAMESPACE_PATTERN.fullmatch(relative.parts[0])
    ):
        namespace = runs_root / relative.parts[0]
        if namespace.is_symlink():
            raise Phase1UsageAcceptanceError(
                "implementation run namespace must not be a symlink"
            )
    else:
        raise Phase1UsageAcceptanceError(
            "implementation_run_dir must be direct or below one vN namespace"
        )
    if run_dir.name != implementation_run_id:
        raise Phase1UsageAcceptanceError(
            "implementation_run_dir basename differs from implementation_run_id"
        )
    if not run_dir.is_dir():
        raise Phase1UsageAcceptanceError("implementation_run_dir does not exist")
    identity_path = run_dir / "state" / "run_identity.v1.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise Phase1UsageAcceptanceError(
            "implementation_run_dir lacks an immutable run identity"
        )
    identity = _read_json_object(identity_path, "implementation run identity")
    expected = {
        "run_id": implementation_run_id,
        "taskpack_id": implementation_run_id,
        "run_kind": "implementation",
    }
    if expected_project_key is not None:
        expected["project_key"] = expected_project_key
    mismatches = [
        key for key, value in expected.items() if identity.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageAcceptanceError(
            "implementation run identity mismatch: " + ", ".join(mismatches)
        )
    return run_dir


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        raise Phase1UsageAcceptanceError(
            "Linux boot identity is unavailable"
        ) from exc


def _process_start_ticks():
    try:
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        return int(stat[stat.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError) as exc:
        raise Phase1UsageAcceptanceError(
            "controller process start identity is unavailable"
        ) from exc


def _fsync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now():
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _print_json(value, stream=None):
    (stream or sys.stdout).write(json.dumps(value, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate and publish the Phase 1 candidate usage live gate."
    )
    parser.add_argument("--profile-project-root", required=True)
    parser.add_argument("--candidate-project-root", required=True)
    parser.add_argument("--implementation-run-id", required=True)
    parser.add_argument("--implementation-run-dir")
    parser.add_argument("--gate-epoch", required=True, type=int)
    parser.add_argument("--acceptance-series-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--authorize-live-call", action="store_true")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_smoke_runtime.DEFAULT_TIMEOUT_SECONDS,
    )
    args = parser.parse_args(argv)
    try:
        summary = run_acceptance(
            profile_project_root=args.profile_project_root,
            candidate_project_root=args.candidate_project_root,
            implementation_run_id=args.implementation_run_id,
            implementation_run_dir=args.implementation_run_dir,
            gate_epoch=args.gate_epoch,
            acceptance_series_id=args.acceptance_series_id,
            attempt_id=args.attempt_id,
            work_root=args.work_root,
            expected_commit=args.expected_commit,
            authorize_live_call=args.authorize_live_call,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        _print_json(
            {
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:MAX_FAILURE_TEXT],
            },
            stream=sys.stderr,
        )
        return 1
    _print_json(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

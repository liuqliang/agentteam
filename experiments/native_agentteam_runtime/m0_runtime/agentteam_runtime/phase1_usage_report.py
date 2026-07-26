"""Deterministic Phase 1 milestone report rendering and external finalization."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import agentteam as _gate_runtime
from . import phase1_usage_acceptance as _acceptance_runtime
from . import profile as _profile_runtime
from . import projection_db as _projection_runtime


GATE_ID = "P1-06E"
DEPENDENCY_GATE_ID = "P1-LIVE"
REPORT_EVIDENCE_SCHEMA_VERSION = "phase1_usage_report_evidence.v1"
FINALIZATION_SCHEMA_VERSION = "phase1_usage_finalization.v1"
FIXED_ARTIFACT_RELATIVE = Path(
    "acceptance/phase1-usage-finalization.v1.json"
)
REPORT_RELATIVE_PATH = Path(
    "experiments/native_agentteam_runtime/implementation_artifacts/"
    "reports/phase1-model-invocation-usage.md"
)
ROADMAP_RELATIVE_PATH = Path(
    "experiments/native_agentteam_runtime/implementation_artifacts/"
    "native_runtime_roadmap.md"
)
FIXED_CHANGED_PATHS = (
    REPORT_RELATIVE_PATH.as_posix(),
    ROADMAP_RELATIVE_PATH.as_posix(),
)
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ROADMAP_START = "<!-- phase1-model-invocation-usage:start -->"
_ROADMAP_END = "<!-- phase1-model-invocation-usage:end -->"
DETERMINISTIC_FIXTURE_TOKEN_TOTALS = {
    "input_tokens": 270,
    "cached_input_tokens": 45,
    "output_tokens": 75,
    "reasoning_tokens": 30,
    "total_tokens": 345,
}


class Phase1UsageReportError(RuntimeError):
    """The Phase 1 report controller rejected unsafe or stale input."""


def build_report_evidence(
    *,
    work_root,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    selected_acceptance_run_id,
    validated_code_sha,
    git_object_format,
    gate_epoch_sha256,
    p1_live_receipt_path,
):
    """Collect bounded canonical evidence for byte-stable milestone rendering."""
    work_root = Path(work_root).resolve()
    implementation_run_id = _safe_slug(
        implementation_run_id, "implementation_run_id"
    )
    acceptance_series_id = _safe_slug(
        acceptance_series_id, "acceptance_series_id"
    )
    selected_acceptance_run_id = _safe_slug(
        selected_acceptance_run_id, "selected_acceptance_run_id"
    )
    _require_git_oid(
        validated_code_sha,
        git_object_format,
        "validated_code_sha",
    )
    if not _SHA256.fullmatch(str(gate_epoch_sha256)):
        raise Phase1UsageReportError("gate_epoch_sha256 is not a SHA-256 digest")

    implementation_run_dir = work_root / "runs" / implementation_run_id
    deterministic_evidence = _deterministic_completion_evidence(
        implementation_run_dir
    )
    if deterministic_evidence["status"] != "passed":
        raise Phase1UsageReportError(
            "deterministic implementation evidence is not complete"
        )

    selected_run_dir = work_root / "runs" / selected_acceptance_run_id
    selected_live_path = (
        selected_run_dir / _acceptance_runtime.FIXED_ARTIFACT_RELATIVE
    )
    selected_live = _read_json_object(
        selected_live_path,
        "selected P1-LIVE artifact",
    )
    _validate_selected_live_artifact(
        selected_live,
        implementation_run_id=implementation_run_id,
        gate_epoch=gate_epoch,
        acceptance_series_id=acceptance_series_id,
        selected_acceptance_run_id=selected_acceptance_run_id,
        validated_code_sha=validated_code_sha,
    )

    projection_check = _projection_runtime.check_project_projection_db(
        work_root
    )
    try:
        projection_replay = _projection_replay_contract(projection_check)
    except Phase1UsageReportError:
        _projection_runtime.rebuild_project_projection_db(work_root)
        projection_check = _projection_runtime.check_project_projection_db(
            work_root
        )
        projection_replay = _projection_replay_contract(projection_check)
    projection = _projection_runtime.build_project_stats(
        work_root,
        filters={
            "implementation_run_id": implementation_run_id,
            "gate_epoch": gate_epoch,
        },
    )
    invocation_usage = projection.get("model_invocation_usage")
    if not isinstance(invocation_usage, dict):
        raise Phase1UsageReportError(
            "canonical model invocation projection is unavailable"
        )
    if invocation_usage.get("open_invocations") != 0:
        raise Phase1UsageReportError(
            "open model invocations block milestone rendering"
        )

    attempts = _acceptance_attempt_evidence(
        work_root,
        implementation_run_id=implementation_run_id,
        gate_epoch=gate_epoch,
        acceptance_series_id=acceptance_series_id,
        selected_acceptance_run_id=selected_acceptance_run_id,
    )
    if not attempts:
        raise Phase1UsageReportError(
            "the explicit acceptance series has no canonical attempts"
        )

    lineage = _projection_runtime.read_projected_follow_up_lineage(work_root)
    verification_summary = _verification_summary(
        deterministic_evidence,
        projection_replay,
        lineage,
    )
    changed_files = sorted(
        {
            path
            for result in (
                lineage.get("worker_results", [])
                if isinstance(lineage, dict)
                else []
            )
            if result.get("run_id") == implementation_run_id
            for path in result.get("changed_files", [])
            if isinstance(path, str)
        }
    )
    stage_breakdown = invocation_usage.get("stage_breakdown")
    if not isinstance(stage_breakdown, dict):
        stage_breakdown = {}
    implemented_paths = sorted(
        key
        for key, value in stage_breakdown.items()
        if isinstance(value, dict) and value.get("invocation_count", 0) > 0
    )
    reason_counts = invocation_usage.get("reason_counts")
    if not isinstance(reason_counts, dict):
        reason_counts = {}
    unavailable = reason_counts.get("unavailable")
    unsupported_or_unavailable = sorted(
        str(item.get("reason") or item)
        for item in (unavailable if isinstance(unavailable, list) else [])
    )

    p1_live_receipt_path = Path(p1_live_receipt_path).resolve()
    p1_live_receipt = _read_json_object(
        p1_live_receipt_path,
        "P1-LIVE receipt",
    )
    if p1_live_receipt.get("evidence_run_id") != selected_acceptance_run_id:
        raise Phase1UsageReportError(
            "P1-LIVE receipt does not select the requested acceptance run"
        )

    attempts_digest = _sha256_json(attempts)
    deterministic_digest = _sha256_json(deterministic_evidence)
    projection_digest = invocation_usage.get(
        "authority_invocation_digest"
    )
    if not _SHA256.fullmatch(str(projection_digest)):
        raise Phase1UsageReportError(
            "projection invocation authority digest is unavailable"
        )
    evidence_digests = {
        "acceptance_attempts_sha256": attempts_digest,
        "deterministic_verification_sha256": deterministic_digest,
        "gate_epoch_sha256": gate_epoch_sha256,
        "p1_live_receipt_sha256": _sha256_bytes(
            p1_live_receipt_path.read_bytes()
        ),
        "projection_invocation_sha256": projection_digest,
        "selected_live_artifact_sha256": _sha256_bytes(
            selected_live_path.read_bytes()
        ),
    }
    evidence = {
        "schema_version": REPORT_EVIDENCE_SCHEMA_VERSION,
        "authority": "canonical_deterministic_and_p1_live",
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "selected_acceptance_run_id": selected_acceptance_run_id,
        "git_object_format": git_object_format,
        "validated_code_sha": validated_code_sha,
        "evidence_digests": evidence_digests,
        "deterministic_evidence": deterministic_evidence,
        "projection_summary": {
            "projection_source": (
                "authoritative_files_with_replay_validation"
            ),
            "check_status": projection_replay["check_status"],
            "schema_version": projection_replay["schema_version"],
            "invocation_count": invocation_usage.get("invocation_count"),
            "open_invocations": invocation_usage.get("open_invocations"),
            "lifecycle_terminal_coverage": invocation_usage.get(
                "lifecycle_terminal_coverage"
            ),
            "token_usage_coverage": invocation_usage.get(
                "token_usage_coverage"
            ),
            "reported_token_totals": invocation_usage.get(
                "reported_token_totals"
            ),
            "partial_known_token_lower_bounds": invocation_usage.get(
                "partial_known_token_lower_bounds"
            ),
            "usage_status_counts": invocation_usage.get(
                "usage_status_counts"
            ),
        },
        "acceptance_attempts": attempts,
        "verification_summary": verification_summary,
        "deterministic_fixture_token_totals": dict(
            DETERMINISTIC_FIXTURE_TOKEN_TOTALS
        ),
        "implemented_invocation_paths": implemented_paths,
        "unsupported_or_unavailable_paths": unsupported_or_unavailable,
        "changed_files": changed_files,
        "remaining_risks": [
            "P1-06E operator approval remains required before source integration",
            "the controller does not merge, push, or activate a runtime release",
        ],
    }
    _validate_report_evidence(evidence)
    return evidence


def render_milestone_report(evidence):
    """Render a byte-stable source report that never embeds its own commit."""
    _validate_report_evidence(evidence)
    attempts = sorted(
        evidence["acceptance_attempts"],
        key=lambda item: (item["run_id"], item["acceptance_attempt_id"]),
    )
    projection = evidence["projection_summary"]
    verification = evidence["verification_summary"]
    lines = [
        "# Phase 1 Model Invocation Usage Milestone Report",
        "",
        "Milestone status: `finalization_pending`",
        "",
        (
            "This report is rendered from canonical deterministic evidence and "
            "controller-validated P1-LIVE evidence. Its merge recommendation "
            "remains conditional until the external P1-06E finalization artifact "
            "and immutable operator approval both validate."
        ),
        "",
        "## Evidence bindings",
        "",
        f"- implementation_run_id: `{evidence['implementation_run_id']}`",
        f"- gate_epoch: `{evidence['gate_epoch']}`",
        f"- acceptance_series_id: `{evidence['acceptance_series_id']}`",
        (
            "- selected_acceptance_run_id: "
            f"`{evidence['selected_acceptance_run_id']}`"
        ),
        f"- git_object_format: `{evidence['git_object_format']}`",
        f"- validated_code_sha: `{evidence['validated_code_sha']}`",
    ]
    lines.extend(
        f"- {key}: `{value}`"
        for key, value in sorted(evidence["evidence_digests"].items())
    )
    lines.extend(
        [
            "",
            "## Implemented invocation paths",
            "",
        ]
    )
    lines.extend(
        _markdown_items(
            evidence["implemented_invocation_paths"],
            empty="No supported invocation path was projected.",
        )
    )
    lines.extend(
        [
            "",
            "## Unsupported or unavailable paths",
            "",
        ]
    )
    lines.extend(
        _markdown_items(
            evidence["unsupported_or_unavailable_paths"],
            empty="None recorded by canonical evidence.",
        )
    )
    lines.extend(
        [
            "",
            "## Coverage result",
            "",
            (
                "- lifecycle_terminal_coverage: "
                f"`{_compact_json(projection['lifecycle_terminal_coverage'])}`"
            ),
            (
                "- token_usage_coverage: "
                f"`{_compact_json(projection['token_usage_coverage'])}`"
            ),
            (
                "- reported_token_totals: "
                f"`{_compact_json(projection['reported_token_totals'])}`"
            ),
            (
                "- partial_known_token_lower_bounds: "
                f"`{_compact_json(projection['partial_known_token_lower_bounds'])}`"
            ),
            f"- open_invocations: `{projection['open_invocations']}`",
            (
                "- exact_deterministic_fixture_token_totals: "
                f"`{_compact_json(evidence['deterministic_fixture_token_totals'])}`"
            ),
            "",
            "## Acceptance series attempts",
            "",
            "| Run | Attempt | Selected | Controller status | Usage |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for attempt in attempts:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(attempt["run_id"]),
                    _markdown_cell(attempt["acceptance_attempt_id"]),
                    "yes" if attempt["selected"] else "no",
                    _markdown_cell(attempt["controller_status"]),
                    _markdown_cell(_compact_json(attempt["usage"])),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Projection replay and deterministic verification",
            "",
            f"- projection_source: `{projection['projection_source']}`",
            f"- projection_check_status: `{projection['check_status']}`",
            (
                "- deterministic_completion_status: "
                f"`{verification['deterministic_completion_status']}`"
            ),
            (
                "- task_result_count: "
                f"`{verification['task_result_count']}`"
            ),
            (
                "- integration_verification_passed_count: "
                f"`{verification['integration_verification_passed_count']}`"
            ),
            (
                "- verification_command_sha256: "
                f"`{verification['verification_command_sha256']}`"
            ),
            (
                "- verification_result_sha256: "
                f"`{verification['verification_result_sha256']}`"
            ),
            "",
            "## Changed files",
            "",
        ]
    )
    lines.extend(
        _markdown_items(
            evidence["changed_files"],
            empty="No implementation changed-file list was projected.",
        )
    )
    lines.extend(
        [
            "",
            "## Remaining risks",
            "",
        ]
    )
    lines.extend(_markdown_items(evidence["remaining_risks"]))
    lines.extend(
        [
            "",
            "## Conditional merge recommendation",
            "",
            (
                "Do not merge yet. The source state is `finalization_pending`. "
                "Proceed only if the external finalization artifact validates "
                "the report-only commit lineage and P1-06E reaches "
                "`awaiting_operator_review`; source integration remains blocked "
                "until the matching immutable operator approval passes."
            ),
            "",
        ]
    )
    rendered = "\n".join(lines).encode("utf-8")
    if b"final_report_sha" in rendered:
        raise Phase1UsageReportError(
            "source report attempted to contain a self-referential commit field"
        )
    return rendered


def render_phase1_usage_report(evidence):
    """Compatibility spelling for the deterministic milestone renderer."""
    return render_milestone_report(evidence)


def render_roadmap_status(roadmap_source, evidence):
    """Replace one bounded roadmap status block deterministically."""
    _validate_report_evidence(evidence)
    if isinstance(roadmap_source, bytes):
        source = roadmap_source.decode("utf-8")
    else:
        source = str(roadmap_source)
    block = "\n".join(
        [
            _ROADMAP_START,
            "### Phase 1 model invocation usage",
            "",
            "- Status: `finalization_pending`",
            f"- Validated code: `{evidence['validated_code_sha']}`",
            (
                "- Evidence digest: "
                f"`{_sha256_json(evidence['evidence_digests'])}`"
            ),
            (
                "- Merge recommendation: conditional; wait for the external "
                "P1-06E finalization artifact and immutable operator approval."
            ),
            _ROADMAP_END,
        ]
    )
    start = source.find(_ROADMAP_START)
    end = source.find(_ROADMAP_END)
    if (start < 0) != (end < 0) or (
        start >= 0 and source.find(_ROADMAP_START, start + 1) >= 0
    ):
        raise Phase1UsageReportError(
            "roadmap contains an ambiguous Phase 1 status marker"
        )
    if start >= 0:
        end += len(_ROADMAP_END)
        source = source[:start] + block + source[end:]
    else:
        source = source.rstrip() + "\n\n" + block
    return (source.rstrip() + "\n").encode("utf-8")


def render_report_artifacts(evidence, roadmap_source):
    """Return the exact two source artifacts keyed by repository-relative path."""
    return {
        REPORT_RELATIVE_PATH.as_posix(): render_milestone_report(evidence),
        ROADMAP_RELATIVE_PATH.as_posix(): render_roadmap_status(
            roadmap_source,
            evidence,
        ),
    }


def validate_report_only_commit(
    candidate_project_root,
    *,
    validated_code_sha,
    final_report_sha=None,
    expected_evidence_digests=None,
    expected_artifacts=None,
):
    """Fail closed unless HEAD is the exact clean report-only child commit."""
    candidate_project_root = Path(candidate_project_root).resolve()
    object_format = _git_object_format(candidate_project_root)
    _require_git_oid(
        validated_code_sha,
        object_format,
        "validated_code_sha",
    )
    head = _git_stdout(candidate_project_root, ["rev-parse", "HEAD"])
    final_report_sha = final_report_sha or head
    _require_git_oid(final_report_sha, object_format, "final_report_sha")
    if head != final_report_sha:
        raise Phase1UsageReportError(
            "final_report_sha is not the current integration HEAD"
        )
    if _git_bytes(
        candidate_project_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ):
        raise Phase1UsageReportError(
            "report finalization requires a clean integration worktree"
        )
    parents = _git_stdout(
        candidate_project_root,
        ["rev-list", "--parents", "-n", "1", final_report_sha],
    ).split()
    if len(parents) != 2:
        raise Phase1UsageReportError(
            "final report commit must have exactly one parent"
        )
    if parents[1] != validated_code_sha:
        raise Phase1UsageReportError(
            "final report commit parent differs from validated_code_sha"
        )
    changed_paths = sorted(
        path
        for path in _git_bytes(
            candidate_project_root,
            [
                "diff",
                "--name-only",
                "-z",
                f"{validated_code_sha}..{final_report_sha}",
            ],
        )
        .decode("utf-8")
        .split("\0")
        if path
    )
    if changed_paths != sorted(FIXED_CHANGED_PATHS):
        raise Phase1UsageReportError(
            "report-only commit contains paths outside the fixed report scope"
        )

    committed = {
        relative: _git_bytes(
            candidate_project_root,
            ["show", f"{final_report_sha}:{relative}"],
        )
        for relative in FIXED_CHANGED_PATHS
    }
    report = committed[REPORT_RELATIVE_PATH.as_posix()]
    if final_report_sha.encode("ascii") in report or b"final_report_sha" in report:
        raise Phase1UsageReportError(
            "source report contains a self-referential final commit"
        )
    if validated_code_sha.encode("ascii") not in report:
        raise Phase1UsageReportError(
            "source report does not bind validated_code_sha"
        )
    for name, digest in sorted((expected_evidence_digests or {}).items()):
        if not _SHA256.fullmatch(str(digest)):
            raise Phase1UsageReportError(
                f"expected evidence digest is invalid: {name}"
            )
        if digest.encode("ascii") not in report:
            raise Phase1UsageReportError(
                f"source report evidence digest mismatch: {name}"
            )
    if expected_artifacts is not None:
        for path in FIXED_CHANGED_PATHS:
            if committed[path] != expected_artifacts[path]:
                raise Phase1UsageReportError(
                    f"committed report artifact was tampered: {path}"
                )
    return {
        "git_object_format": object_format,
        "validated_code_sha": validated_code_sha,
        "parent_commit_sha": parents[1],
        "final_report_sha": final_report_sha,
        "changed_paths": changed_paths,
        "report_sha256": _sha256_bytes(report),
        "roadmap_sha256": _sha256_bytes(
            committed[ROADMAP_RELATIVE_PATH.as_posix()]
        ),
    }


def complete_phase1_usage_report(
    *,
    profile_project_root,
    candidate_project_root,
    implementation_run_id,
    gate_epoch,
    work_root,
    acceptance_series_id,
    run_id,
    validated_code_sha,
    final_publisher=None,
):
    """Create/resume the report-only commit and publish external finalization."""
    implementation_run_id = _safe_slug(
        implementation_run_id, "implementation_run_id"
    )
    acceptance_series_id = _safe_slug(
        acceptance_series_id, "acceptance_series_id"
    )
    run_id = _safe_slug(run_id, "run_id")
    if not isinstance(gate_epoch, int) or isinstance(gate_epoch, bool) or gate_epoch < 1:
        raise Phase1UsageReportError("gate_epoch must be a positive integer")

    profile_project_root = Path(profile_project_root).resolve()
    candidate_project_root = Path(candidate_project_root).resolve()
    supplied_work_root = Path(work_root).resolve()
    profile = _profile_runtime.load_project_profile(profile_project_root)
    configured_work_root = Path(profile["work_root"]).resolve()
    if supplied_work_root != configured_work_root:
        raise Phase1UsageReportError(
            "supplied work_root differs from the explicit project profile"
        )
    _acceptance_runtime._require_same_git_repository(
        profile_project_root,
        candidate_project_root,
    )
    _acceptance_runtime._verify_candidate_runtime_modules(
        candidate_project_root
    )
    implementation_run_dir = configured_work_root / "runs" / implementation_run_id
    context = _gate_runtime._require_post_backlog_gate_context(
        profile,
        implementation_run_dir,
    )
    _acceptance_runtime._require_same_git_repository(
        context["project_root"],
        candidate_project_root,
    )
    declaration = _gate_runtime._require_gate_declaration(context, GATE_ID)
    live_declaration = _gate_runtime._require_gate_declaration(
        context,
        DEPENDENCY_GATE_ID,
    )
    _validate_gate_declarations(declaration, live_declaration)

    initial_epoch = _gate_runtime._require_current_gate_epoch(
        context,
        gate_epoch,
    )
    _validate_epoch_binding(
        context,
        initial_epoch,
        candidate_project_root,
        validated_code_sha,
    )
    selected_run_dir = configured_work_root / "runs" / run_id
    final_path = selected_run_dir / FIXED_ARTIFACT_RELATIVE

    try:
        with _gate_runtime._gate_mutation_locks(context, [GATE_ID]):
            current = _gate_runtime._require_current_gate_epoch(
                context,
                gate_epoch,
            )
            if current["digest"] != initial_epoch["digest"]:
                raise Phase1UsageReportError(
                    "gate epoch changed before report completion"
            )
            _validate_epoch_binding(
                context,
                current,
                candidate_project_root,
                validated_code_sha,
            )
            _require_live_gate_passed(
                context,
                current,
                live_declaration,
                selected_run_id=run_id,
                validated_code_sha=validated_code_sha,
            )
            receipt_path = _register_pending_receipt(
                context,
                current,
                declaration,
                evidence_run_id=run_id,
                validated_code_sha=validated_code_sha,
            )
            existing = _read_json_if_exists(final_path)
            if existing:
                _validate_finalization_artifact(existing)
                lineage = validate_report_only_commit(
                    candidate_project_root,
                    validated_code_sha=validated_code_sha,
                    final_report_sha=existing.get("final_report_sha"),
                    expected_evidence_digests=existing.get(
                        "evidence_digests"
                    ),
                )
                _validate_existing_finalization_evidence(
                    existing,
                    lineage=lineage,
                    work_root=configured_work_root,
                    implementation_run_id=implementation_run_id,
                    gate_epoch=gate_epoch,
                    acceptance_series_id=acceptance_series_id,
                    selected_acceptance_run_id=run_id,
                    gate_epoch_sha256=current["digest"],
                    p1_live_receipt_path=_gate_runtime._gate_receipt_path(
                        context,
                        current["record"],
                        DEPENDENCY_GATE_ID,
                    ),
                )
                return {
                    "status": "passed",
                    "gate_state": _final_gate_state(
                        context,
                        current,
                        declaration,
                    ),
                    "artifact_path": str(final_path),
                    "receipt_path": str(receipt_path),
                    "validated_code_sha": validated_code_sha,
                    "final_report_sha": lineage["final_report_sha"],
                    "changed_paths": lineage["changed_paths"],
                    "idempotent": True,
                }
            evidence = build_report_evidence(
                work_root=configured_work_root,
                implementation_run_id=implementation_run_id,
                gate_epoch=gate_epoch,
                acceptance_series_id=acceptance_series_id,
                selected_acceptance_run_id=run_id,
                validated_code_sha=validated_code_sha,
                git_object_format=current["record"]["git_object_format"],
                gate_epoch_sha256=current["digest"],
                p1_live_receipt_path=_gate_runtime._gate_receipt_path(
                    context,
                    current["record"],
                    DEPENDENCY_GATE_ID,
                ),
            )
            head = _git_stdout(
                candidate_project_root,
                ["rev-parse", "HEAD"],
            )
            ordinary_status = _git_bytes(
                candidate_project_root,
                [
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
            )
            if ordinary_status:
                raise Phase1UsageReportError(
                    "report completion requires a clean integration worktree"
                )

            roadmap_path = candidate_project_root / ROADMAP_RELATIVE_PATH
            if not roadmap_path.is_file():
                raise Phase1UsageReportError(
                    "the fixed native runtime roadmap is missing"
                )
            artifacts = render_report_artifacts(
                evidence,
                roadmap_path.read_bytes(),
            )
            if head == validated_code_sha:
                _write_and_commit_report_artifacts(
                    candidate_project_root,
                    artifacts,
                    implementation_run_id=implementation_run_id,
                    gate_epoch=gate_epoch,
                )
                head = _git_stdout(
                    candidate_project_root,
                    ["rev-parse", "HEAD"],
                )
            else:
                _validate_pending_receipt(
                    receipt_path,
                    context,
                    current,
                    declaration,
                    evidence_run_id=run_id,
                    validated_code_sha=validated_code_sha,
                )

            lineage = validate_report_only_commit(
                candidate_project_root,
                validated_code_sha=validated_code_sha,
                final_report_sha=head,
                expected_evidence_digests=evidence["evidence_digests"],
                expected_artifacts=artifacts,
            )
            verification_summary = {
                "deterministic_completion_status": (
                    evidence["verification_summary"][
                        "deterministic_completion_status"
                    ]
                ),
                "live_validation_status": "passed",
                "projection_status": evidence["projection_summary"][
                    "check_status"
                ],
                "open_invocations": evidence["projection_summary"][
                    "open_invocations"
                ],
                "acceptance_attempt_count": len(
                    evidence["acceptance_attempts"]
                ),
            }
            candidate_artifact = {
                "schema_version": FINALIZATION_SCHEMA_VERSION,
                "implementation_run_id": implementation_run_id,
                "gate_epoch": gate_epoch,
                "acceptance_series_id": acceptance_series_id,
                "selected_acceptance_run_id": run_id,
                "git_object_format": lineage["git_object_format"],
                "validated_code_sha": validated_code_sha,
                "parent_commit_sha": lineage["parent_commit_sha"],
                "final_report_sha": lineage["final_report_sha"],
                "changed_paths": lineage["changed_paths"],
                "report_sha256": lineage["report_sha256"],
                "roadmap_sha256": lineage["roadmap_sha256"],
                "evidence_digests": evidence["evidence_digests"],
                "verification_summary": verification_summary,
                "merge_recommendation": "ready_for_operator_review",
                "controller_validation_status": "passed",
                "created_at": _utc_now(),
            }
            _validate_finalization_artifact(candidate_artifact)
            final_epoch = _gate_runtime._require_current_gate_epoch(
                context,
                gate_epoch,
            )
            if final_epoch["digest"] != current["digest"]:
                raise Phase1UsageReportError(
                    "gate epoch changed before finalization publication"
                )
            final_lineage = validate_report_only_commit(
                candidate_project_root,
                validated_code_sha=validated_code_sha,
                final_report_sha=head,
                expected_evidence_digests=evidence["evidence_digests"],
                expected_artifacts=artifacts,
            )
            if final_lineage != lineage:
                raise Phase1UsageReportError(
                    "report-only commit changed before final publication"
                )
            publisher = final_publisher or _publish_final_artifact
            publisher(final_path, candidate_artifact)
            gate_state = _final_gate_state(
                context,
                final_epoch,
                declaration,
            )
            if gate_state not in {"awaiting_operator_review", "passed"}:
                raise Phase1UsageReportError(
                    "published finalization did not reach operator review"
                )
            return {
                "status": "passed",
                "gate_state": gate_state,
                "artifact_path": str(final_path),
                "receipt_path": str(receipt_path),
                "validated_code_sha": validated_code_sha,
                "final_report_sha": head,
                "changed_paths": lineage["changed_paths"],
                "idempotent": False,
            }
    except Phase1UsageReportError:
        raise
    except Exception as exc:
        raise Phase1UsageReportError(str(exc)) from exc


def run_completion(**kwargs):
    """Compatibility spelling for the complete controller."""
    return complete_phase1_usage_report(**kwargs)


def _deterministic_completion_evidence(run_dir):
    state_path = Path(run_dir) / "state" / "two_phase_scheduler_state.json"
    state = _read_json_object(
        state_path,
        "implementation scheduler state",
    )
    scheduler_status = state.get("scheduler_status")
    steps = state.get("steps")
    if not isinstance(steps, list) or not steps:
        return {
            "status": "failed",
            "scheduler_status": scheduler_status,
            "task_results": [],
        }
    task_results = []
    passed = scheduler_status in {
        "idle",
        "awaiting_post_backlog_gates",
    }
    for step in steps:
        result = step.get("result") if isinstance(step, dict) else None
        result = result if isinstance(result, dict) else {}
        summary = {
            "task_id": step.get("task_id"),
            "attempt_id": step.get("attempt_id"),
            "result_status": result.get("result_status"),
            "integration_status": result.get("integration_status"),
            "integration_verification_status": result.get(
                "integration_verification_status"
            ),
            "integration_verification_additions_status": result.get(
                "integration_verification_additions_status"
            ),
        }
        task_results.append(summary)
        if summary["result_status"] != "completed":
            passed = False
        if summary["integration_status"] not in {None, "passed"}:
            passed = False
        if summary["integration_verification_status"] not in {None, "passed"}:
            passed = False
        if summary["integration_verification_additions_status"] not in {
            None,
            "passed",
        }:
            passed = False
    task_results.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("attempt_id") or ""),
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "scheduler_status": scheduler_status,
        "task_results": task_results,
        "state_sha256": _sha256_bytes(state_path.read_bytes()),
    }


def _acceptance_attempt_evidence(
    work_root,
    *,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    selected_acceptance_run_id,
):
    attempts = []
    runs_root = Path(work_root) / "runs"
    for run_dir in sorted(
        path for path in runs_root.iterdir() if path.is_dir()
    ):
        identity = _read_json_if_exists(
            run_dir / "state" / "run_identity.v1.json"
        )
        if (
            identity.get("run_kind") != "acceptance_evidence"
            or identity.get("implementation_run_id") != implementation_run_id
            or identity.get("gate_epoch") != gate_epoch
        ):
            continue
        claim = _read_json_if_exists(
            run_dir / "state" / "controller_claim.v1.json"
        )
        if (
            claim.get("run_id") != run_dir.name
            or claim.get("implementation_run_id") != implementation_run_id
            or claim.get("gate_epoch") != gate_epoch
        ):
            raise Phase1UsageReportError(
                f"acceptance attempt claim binding mismatch: {run_dir.name}"
            )
        series = claim.get("acceptance_series_id")
        if not isinstance(series, str):
            raise Phase1UsageReportError(
                f"acceptance attempt lacks explicit series metadata: {run_dir.name}"
            )
        if series != acceptance_series_id:
            continue
        attempt_id = claim.get("acceptance_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise Phase1UsageReportError(
                f"acceptance attempt lacks explicit attempt metadata: {run_dir.name}"
            )
        live = _read_json_if_exists(
            run_dir / _acceptance_runtime.FIXED_ARTIFACT_RELATIVE
        )
        failure = _read_json_if_exists(
            run_dir / _acceptance_runtime.FAILURE_ARTIFACT_RELATIVE
        )
        controller = _read_json_if_exists(
            run_dir / "state" / "controller_result.v1.json"
        )
        controller_status = (
            "passed"
            if live.get("controller_validation_status") == "passed"
            else controller.get("status")
            or failure.get("status")
            or "incomplete"
        )
        stats = _projection_runtime.build_project_stats(
            work_root,
            filters={"run_id": run_dir.name},
        )
        usage = stats.get("model_invocation_usage")
        if not isinstance(usage, dict):
            raise Phase1UsageReportError(
                f"acceptance attempt usage is unavailable: {run_dir.name}"
            )
        attempts.append(
            {
                "run_id": run_dir.name,
                "acceptance_series_id": series,
                "acceptance_attempt_id": attempt_id,
                "selected": run_dir.name == selected_acceptance_run_id,
                "controller_status": str(controller_status),
                "usage": {
                    "invocation_count": usage.get("invocation_count"),
                    "open_invocations": usage.get("open_invocations"),
                    "usage_status_counts": usage.get("usage_status_counts"),
                    "reported_token_totals": usage.get(
                        "reported_token_totals"
                    ),
                    "partial_known_token_lower_bounds": usage.get(
                        "partial_known_token_lower_bounds"
                    ),
                },
            }
        )
    attempts.sort(
        key=lambda item: (item["run_id"], item["acceptance_attempt_id"])
    )
    selected = [item for item in attempts if item["selected"]]
    if len(selected) != 1 or selected[0]["controller_status"] != "passed":
        raise Phase1UsageReportError(
            "selected P1-LIVE attempt is not one explicit passed attempt"
        )
    return attempts


def _projection_replay_contract(projection_check):
    """Accept a fresh replay or staleness caused only by gate metadata writes."""
    if projection_check.get("check_status") == "passed":
        return {
            "check_status": "passed",
            "schema_version": projection_check.get("schema_version"),
            "replay_detail": "fresh",
        }
    mismatches = set(projection_check.get("mismatches") or [])
    metadata_only = {
        "artifacts",
        "artifact_bytes",
        "artifact_digest",
    }
    expected = projection_check.get("expected")
    actual = projection_check.get("actual")
    invocation_matches = (
        isinstance(expected, dict)
        and isinstance(actual, dict)
        and expected.get("invocations") == actual.get("invocations")
        and expected.get("invocation_digest")
        == actual.get("invocation_digest")
    )
    if mismatches and mismatches <= metadata_only and invocation_matches:
        return {
            "check_status": "passed",
            "schema_version": projection_check.get("schema_version"),
            "replay_detail": "gate_metadata_only_staleness",
        }
    raise Phase1UsageReportError(
        "the project projection must be rebuilt and replay-stable"
    )


def _verification_summary(deterministic, projection_replay, lineage):
    integration_outcomes = (
        lineage.get("integration_outcomes", [])
        if isinstance(lineage, dict)
        else []
    )
    passed_count = sum(
        1
        for item in integration_outcomes
        if item.get("integration_verification_status") == "passed"
    )
    return {
        "deterministic_completion_status": deterministic["status"],
        "task_result_count": len(deterministic["task_results"]),
        "integration_verification_passed_count": passed_count,
        "projection_replay_status": projection_replay.get("check_status"),
        "verification_command_sha256": _sha256_json(
            [
                item.get("declared_verification_additions", [])
                for item in integration_outcomes
            ]
        ),
        "verification_result_sha256": _sha256_json(
            [
                {
                    "run_id": item.get("run_id"),
                    "task_id": item.get("task_id"),
                    "attempt_id": item.get("attempt_id"),
                    "status": item.get("integration_verification_status"),
                    "exit_code": item.get(
                        "integration_verification_exit_code"
                    ),
                }
                for item in integration_outcomes
            ]
        ),
    }


def _validate_selected_live_artifact(
    artifact,
    *,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    selected_acceptance_run_id,
    validated_code_sha,
):
    _acceptance_runtime._validate_live_artifact_schema(artifact)
    expected = {
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "run_id": selected_acceptance_run_id,
        "validated_code_sha": validated_code_sha,
        "candidate_commit_sha": validated_code_sha,
        "controller_validation_status": "passed",
    }
    mismatches = [
        key for key, value in expected.items() if artifact.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageReportError(
            "selected P1-LIVE artifact binding mismatch: "
            + ", ".join(mismatches)
        )


def _validate_report_evidence(evidence):
    if not isinstance(evidence, dict):
        raise Phase1UsageReportError("report evidence must be an object")
    required = {
        "schema_version",
        "authority",
        "implementation_run_id",
        "gate_epoch",
        "acceptance_series_id",
        "selected_acceptance_run_id",
        "git_object_format",
        "validated_code_sha",
        "evidence_digests",
        "deterministic_evidence",
        "projection_summary",
        "acceptance_attempts",
        "verification_summary",
        "deterministic_fixture_token_totals",
        "implemented_invocation_paths",
        "unsupported_or_unavailable_paths",
        "changed_files",
        "remaining_risks",
    }
    missing = sorted(required - set(evidence))
    if missing:
        raise Phase1UsageReportError(
            "report evidence is missing: " + ", ".join(missing)
        )
    if evidence["schema_version"] != REPORT_EVIDENCE_SCHEMA_VERSION:
        raise Phase1UsageReportError("unsupported report evidence schema")
    if evidence["authority"] != "canonical_deterministic_and_p1_live":
        raise Phase1UsageReportError(
            "report evidence is not canonical deterministic and P1-LIVE evidence"
        )
    _require_git_oid(
        evidence["validated_code_sha"],
        evidence["git_object_format"],
        "validated_code_sha",
    )
    if evidence["deterministic_evidence"].get("status") != "passed":
        raise Phase1UsageReportError(
            "deterministic completion evidence did not pass"
        )
    if (
        evidence["deterministic_fixture_token_totals"]
        != DETERMINISTIC_FIXTURE_TOKEN_TOTALS
    ):
        raise Phase1UsageReportError(
            "deterministic fixture totals differ from the approved contract"
        )
    digests = evidence["evidence_digests"]
    if not isinstance(digests, dict) or not digests:
        raise Phase1UsageReportError("report evidence digests are missing")
    for name, value in digests.items():
        if not isinstance(name, str) or not _SHA256.fullmatch(str(value)):
            raise Phase1UsageReportError(
                f"report evidence digest is invalid: {name}"
            )
    attempts = evidence["acceptance_attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise Phase1UsageReportError("report requires acceptance attempts")
    selected = []
    for attempt in attempts:
        if attempt.get("acceptance_series_id") != evidence[
            "acceptance_series_id"
        ]:
            raise Phase1UsageReportError(
                "acceptance attempt lacks matching explicit series metadata"
            )
        if attempt.get("selected"):
            selected.append(attempt)
    if (
        len(selected) != 1
        or selected[0].get("run_id")
        != evidence["selected_acceptance_run_id"]
        or selected[0].get("controller_status") != "passed"
    ):
        raise Phase1UsageReportError(
            "report requires one selected passed P1-LIVE attempt"
        )


def _validate_gate_declarations(declaration, live_declaration):
    expected = {
        "evidence_artifact": FIXED_ARTIFACT_RELATIVE.as_posix(),
        "evidence_schema": (
            "experiments/native_agentteam_runtime/schemas/"
            "phase1_usage_finalization.schema.json"
        ),
        "required_status_field": "controller_validation_status",
        "required_status_value": "passed",
        "commit_field": "final_report_sha",
        "integration_head_relation": "equals",
    }
    mismatches = [
        key for key, value in expected.items() if declaration.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageReportError(
            "P1-06E declaration binding mismatch: " + ", ".join(mismatches)
        )
    if DEPENDENCY_GATE_ID not in declaration.get("depends_on", []):
        raise Phase1UsageReportError("P1-06E does not depend on P1-LIVE")
    if (
        live_declaration.get("evidence_artifact")
        != _acceptance_runtime.FIXED_ARTIFACT_RELATIVE.as_posix()
    ):
        raise Phase1UsageReportError(
            "P1-LIVE declaration does not bind its fixed artifact"
        )


def _validate_epoch_binding(
    context,
    current,
    candidate_root,
    validated_code_sha,
):
    record = current["record"]
    object_format = _git_object_format(candidate_root)
    if record.get("git_object_format") != object_format:
        raise Phase1UsageReportError(
            "gate epoch Git object format differs from the candidate"
        )
    _require_git_oid(
        validated_code_sha,
        object_format,
        "validated_code_sha",
    )
    if record.get("validated_code_sha") != validated_code_sha:
        raise Phase1UsageReportError(
            "validated_code_sha differs from the current gate epoch"
        )
    integration_worktree = _gate_runtime._gate_integration_worktree(
        context,
        record,
    )
    if Path(integration_worktree).resolve() != Path(candidate_root).resolve():
        raise Phase1UsageReportError(
            "candidate root is not the current gate integration worktree"
        )
    head = _git_stdout(candidate_root, ["rev-parse", "HEAD"])
    if head == validated_code_sha:
        return
    parents = _git_stdout(
        candidate_root,
        ["rev-list", "--parents", "-n", "1", head],
    ).split()
    if len(parents) != 2 or parents[1] != validated_code_sha:
        raise Phase1UsageReportError(
            "candidate HEAD is neither validated code nor its report-only child"
        )


def _require_live_gate_passed(
    context,
    current,
    declaration,
    *,
    selected_run_id,
    validated_code_sha,
):
    decision = _gate_runtime._evaluate_one_post_backlog_gate(
        context,
        current,
        declaration,
        prior_decisions={},
    )
    if decision.get("state") != "passed":
        raise Phase1UsageReportError(
            "the registered P1-LIVE dependency is not passed"
        )
    receipt_path = _gate_runtime._gate_receipt_path(
        context,
        current["record"],
        DEPENDENCY_GATE_ID,
    )
    receipt = _read_json_object(receipt_path, "P1-LIVE receipt")
    if receipt.get("evidence_run_id") != selected_run_id:
        raise Phase1UsageReportError(
            "selected acceptance run differs from the passed P1-LIVE receipt"
        )
    artifact_path = (
        context["work_root"]
        / receipt["evidence_run_relative_path"]
        / declaration["evidence_artifact"]
    )
    _acceptance_runtime._validate_p1_live_receipt(
        context,
        current,
        declaration,
        receipt_path=receipt_path,
        artifact_path=artifact_path,
        expected_commit=validated_code_sha,
    )


def _register_pending_receipt(
    context,
    current,
    declaration,
    *,
    evidence_run_id,
    validated_code_sha,
):
    receipt_path = _gate_runtime._gate_receipt_path(
        context,
        current["record"],
        GATE_ID,
    )
    if receipt_path.exists():
        _validate_pending_receipt(
            receipt_path,
            context,
            current,
            declaration,
            evidence_run_id=evidence_run_id,
            validated_code_sha=validated_code_sha,
        )
        return receipt_path
    receipt = {
        "schema_version": "post_backlog_gate_receipt.v1",
        "implementation_run_id": context["run_dir"].name,
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "gate_id": GATE_ID,
        "evidence_run_id": evidence_run_id,
        "evidence_run_relative_path": f"runs/{evidence_run_id}",
        "expected_integration_head_sha": validated_code_sha,
        "git_object_format": current["record"]["git_object_format"],
        "evidence_artifact": declaration["evidence_artifact"],
        "evidence_schema": declaration["evidence_schema"],
        "attempt_history": [],
        "registered_at": _utc_now(),
    }
    _gate_runtime._validate_gate_record_schema(
        "post_backlog_gate_receipt.schema.json",
        receipt,
    )
    _gate_runtime._atomic_write_json(receipt_path, receipt)
    return receipt_path


def _validate_pending_receipt(
    receipt_path,
    context,
    current,
    declaration,
    *,
    evidence_run_id,
    validated_code_sha,
):
    receipt = _read_json_object(receipt_path, "P1-06E pending receipt")
    _gate_runtime._validate_gate_record_schema(
        "post_backlog_gate_receipt.schema.json",
        receipt,
    )
    expected = {
        "implementation_run_id": context["run_dir"].name,
        "epoch_number": current["record"]["epoch_number"],
        "epoch_sha256": current["digest"],
        "gate_id": GATE_ID,
        "evidence_run_id": evidence_run_id,
        "evidence_run_relative_path": f"runs/{evidence_run_id}",
        "expected_integration_head_sha": validated_code_sha,
        "git_object_format": current["record"]["git_object_format"],
        "evidence_artifact": declaration["evidence_artifact"],
        "evidence_schema": declaration["evidence_schema"],
    }
    mismatches = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageReportError(
            "P1-06E pending receipt binding mismatch: "
            + ", ".join(mismatches)
        )


def _write_and_commit_report_artifacts(
    candidate_root,
    artifacts,
    *,
    implementation_run_id,
    gate_epoch,
):
    for relative in FIXED_CHANGED_PATHS:
        _atomic_write_bytes(candidate_root / relative, artifacts[relative])
    changed = _worktree_changed_paths(candidate_root)
    if changed != sorted(FIXED_CHANGED_PATHS):
        raise Phase1UsageReportError(
            "render phase did not produce the exact fixed two-path diff"
        )
    _git_stdout(candidate_root, ["add", "--", *FIXED_CHANGED_PATHS])
    staged = sorted(
        path
        for path in _git_bytes(
            candidate_root,
            ["diff", "--cached", "--name-only", "-z"],
        )
        .decode("utf-8")
        .split("\0")
        if path
    )
    if staged != sorted(FIXED_CHANGED_PATHS):
        raise Phase1UsageReportError(
            "staged report diff is outside the fixed two-path scope"
        )
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=AgentTeam deterministic controller",
            "-c",
            "user.email=agentteam-controller@invalid",
            "commit",
            "--no-gpg-sign",
            "-m",
            (
                f"Finalize {implementation_run_id} Phase 1 usage report "
                f"(epoch {gate_epoch})"
            ),
            "--",
            *FIXED_CHANGED_PATHS,
        ],
        cwd=candidate_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise Phase1UsageReportError(
            "report-only commit failed: " + completed.stderr.strip()
        )


def _worktree_changed_paths(candidate_root):
    output = _git_bytes(
        candidate_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    entries = output.decode("utf-8").split("\0")
    paths = []
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4:
            raise Phase1UsageReportError("Git status output is malformed")
        path = entry[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path)
    return sorted(paths)


def _final_gate_state(context, current, declaration):
    decision = _gate_runtime._evaluate_one_post_backlog_gate(
        context,
        current,
        declaration,
        prior_decisions={DEPENDENCY_GATE_ID: {"state": "passed"}},
    )
    return decision.get("state")


def _validate_finalization_artifact(artifact):
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "phase1_usage_finalization.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        import jsonschema

        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
        errors = sorted(
            validator_class(
                schema,
                format_checker=jsonschema.FormatChecker(),
            ).iter_errors(artifact),
            key=lambda error: tuple(
                str(part) for part in error.absolute_path
            ),
        )
    except Exception as exc:
        if exc.__class__.__module__.startswith(
            ("jsonschema", "referencing")
        ):
            raise Phase1UsageReportError(
                f"finalization schema validation failed: {exc}"
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
        raise Phase1UsageReportError(
            "finalization schema validation failed: " + detail
        )


def _validate_existing_finalization_evidence(
    artifact,
    *,
    lineage,
    work_root,
    implementation_run_id,
    gate_epoch,
    acceptance_series_id,
    selected_acceptance_run_id,
    gate_epoch_sha256,
    p1_live_receipt_path,
):
    expected = {
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "acceptance_series_id": acceptance_series_id,
        "selected_acceptance_run_id": selected_acceptance_run_id,
        "git_object_format": lineage["git_object_format"],
        "validated_code_sha": lineage["validated_code_sha"],
        "parent_commit_sha": lineage["parent_commit_sha"],
        "final_report_sha": lineage["final_report_sha"],
        "changed_paths": lineage["changed_paths"],
        "report_sha256": lineage["report_sha256"],
        "roadmap_sha256": lineage["roadmap_sha256"],
    }
    mismatches = [
        key for key, value in expected.items() if artifact.get(key) != value
    ]
    if mismatches:
        raise Phase1UsageReportError(
            "existing finalization artifact binding mismatch: "
            + ", ".join(sorted(mismatches))
        )
    digests = artifact["evidence_digests"]
    receipt_path = Path(p1_live_receipt_path)
    live_path = (
        Path(work_root)
        / "runs"
        / selected_acceptance_run_id
        / _acceptance_runtime.FIXED_ARTIFACT_RELATIVE
    )
    direct_digests = {
        "gate_epoch_sha256": gate_epoch_sha256,
        "p1_live_receipt_sha256": _sha256_bytes(receipt_path.read_bytes()),
        "selected_live_artifact_sha256": _sha256_bytes(
            live_path.read_bytes()
        ),
    }
    direct_mismatches = [
        key for key, value in direct_digests.items() if digests.get(key) != value
    ]
    deterministic = _deterministic_completion_evidence(
        Path(work_root) / "runs" / implementation_run_id
    )
    if (
        deterministic.get("status") != "passed"
        or digests.get("deterministic_verification_sha256")
        != _sha256_json(deterministic)
    ):
        direct_mismatches.append("deterministic_verification_sha256")
    attempts = _acceptance_attempt_evidence(
        Path(work_root),
        implementation_run_id=implementation_run_id,
        gate_epoch=gate_epoch,
        acceptance_series_id=acceptance_series_id,
        selected_acceptance_run_id=selected_acceptance_run_id,
    )
    if digests.get("acceptance_attempts_sha256") != _sha256_json(attempts):
        direct_mismatches.append("acceptance_attempts_sha256")
    stats = _projection_runtime.build_project_stats(
        work_root,
        filters={
            "implementation_run_id": implementation_run_id,
            "gate_epoch": gate_epoch,
        },
    )
    invocation_digest = (
        stats.get("model_invocation_usage", {}).get(
            "authority_invocation_digest"
        )
        if isinstance(stats.get("model_invocation_usage"), dict)
        else None
    )
    if digests.get("projection_invocation_sha256") != invocation_digest:
        direct_mismatches.append("projection_invocation_sha256")
    if direct_mismatches:
        raise Phase1UsageReportError(
            "existing finalization evidence changed: "
            + ", ".join(sorted(set(direct_mismatches)))
        )


def _publish_final_artifact(path, artifact):
    _atomic_write_json(path, artifact, replace=False)


def _atomic_write_json(path, value, *, replace):
    payload = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write_bytes(path, payload, replace=replace)


def _atomic_write_bytes(path, payload, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
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
                raise Phase1UsageReportError(
                    f"immutable finalization artifact already exists: {path}"
                ) from exc
            temporary.unlink()
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json_object(path, label):
    value = _read_json_if_exists(path)
    if not value:
        raise Phase1UsageReportError(f"{label} is missing or invalid: {path}")
    return value


def _read_json_if_exists(path):
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase1UsageReportError(
            f"invalid JSON artifact: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise Phase1UsageReportError(f"JSON artifact is not an object: {path}")
    return value


def _git_object_format(project_root):
    value = _git_stdout(
        project_root,
        ["rev-parse", "--show-object-format"],
    )
    if value not in {"sha1", "sha256"}:
        raise Phase1UsageReportError(
            f"unsupported Git object format: {value}"
        )
    return value


def _require_git_oid(value, object_format, field_name):
    expected_length = 40 if object_format == "sha1" else 64
    if (
        object_format not in {"sha1", "sha256"}
        or not isinstance(value, str)
        or len(value) != expected_length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase1UsageReportError(
            f"{field_name} is not a canonical {object_format} Git OID"
        )


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
        raise Phase1UsageReportError(
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
        raise Phase1UsageReportError(
            f"git {' '.join(arguments)} failed"
        )
    return completed.stdout


def _safe_slug(value, label):
    if not isinstance(value, str) or not SAFE_SLUG.fullmatch(value):
        raise Phase1UsageReportError(f"{label} is not a bounded safe slug")
    return value


def _compact_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256_json(value):
    return _sha256_bytes(_compact_json(value).encode("utf-8"))


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _markdown_items(values, *, empty=None):
    values = [str(value) for value in values]
    if not values:
        return [empty] if empty is not None else []
    return [f"- `{value.replace('`', '')}`" for value in values]


def _markdown_cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _fsync_directory(path):
    descriptor = os.open(
        Path(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _print_json(value, stream=None):
    print(json.dumps(value, indent=2, sort_keys=True), file=stream or sys.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Complete the deterministic Phase 1 usage milestone report"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    complete = subparsers.add_parser("complete")
    complete.add_argument("--profile-project-root", required=True)
    complete.add_argument("--candidate-project-root", required=True)
    complete.add_argument("--implementation-run-id", required=True)
    complete.add_argument("--gate-epoch", required=True, type=int)
    complete.add_argument("--work-root", required=True)
    complete.add_argument("--acceptance-series-id", required=True)
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--validated-code-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = complete_phase1_usage_report(
            profile_project_root=args.profile_project_root,
            candidate_project_root=args.candidate_project_root,
            implementation_run_id=args.implementation_run_id,
            gate_epoch=args.gate_epoch,
            work_root=args.work_root,
            acceptance_series_id=args.acceptance_series_id,
            run_id=args.run_id,
            validated_code_sha=args.validated_code_sha,
        )
    except Phase1UsageReportError as exc:
        _print_json(
            {
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 1
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

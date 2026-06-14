import json
from datetime import UTC, datetime
from pathlib import Path


SEMANTIC_FEEDBACK_SCHEMA_VERSION = "semantic_feedback_proposal.v1"
AUTHORITY_BOUNDARY = (
    "proposal_only: this artifact records implementation feedback for semantic "
    "authority review and does not mutate design authority documents."
)
DEFAULT_TEXT_LIMIT = 1200


def write_semantic_feedback_proposal(
    *,
    work_root,
    proposal_id,
    source_report,
    target_artifacts,
    summary,
    rationale,
    created_by="agentteam-cli",
    created_at=None,
):
    work_root = Path(work_root).resolve()
    proposal_id = _safe_artifact_id(proposal_id)
    source_report = source_report if isinstance(source_report, dict) else {}
    payload = {
        "proposal_schema_version": SEMANTIC_FEEDBACK_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "proposal_status": "pending_review",
        "source_taskpack_id": source_report.get("run_id") or "unknown",
        "source_run_dir": source_report.get("run_dir"),
        "source_report_path": source_report.get("report_path"),
        "target_artifacts": _bounded_text_items(target_artifacts, DEFAULT_TEXT_LIMIT),
        "summary": _bounded_text(summary, DEFAULT_TEXT_LIMIT),
        "rationale": _bounded_text(rationale, DEFAULT_TEXT_LIMIT),
        "authority_boundary": AUTHORITY_BOUNDARY,
        "created_by": _bounded_text(created_by, 120),
        "created_at": created_at or _utc_now(),
    }
    proposal_root = work_root / "semantic_feedback"
    proposal_root.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_root / f"{proposal_id}.json"
    payload["proposal_path"] = str(proposal_path.resolve())
    proposal_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def list_semantic_feedback_proposals(work_root):
    proposal_root = Path(work_root).resolve() / "semantic_feedback"
    proposals = []
    if proposal_root.exists():
        for path in sorted(proposal_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            payload["proposal_path"] = payload.get("proposal_path") or str(path.resolve())
            proposals.append(payload)
    return {
        "feedback_schema_version": "semantic_feedback_list.v1",
        "proposal_root": str(proposal_root.resolve()),
        "proposal_count": len(proposals),
        "proposals": proposals,
    }


def render_semantic_feedback_text(summary):
    lines = [
        f"proposal_count: {summary.get('proposal_count', 0)}",
        f"proposal_root: {summary.get('proposal_root') or 'unknown'}",
    ]
    for proposal in summary.get("proposals") or []:
        if not isinstance(proposal, dict):
            continue
        lines.append(
            "proposal: "
            f"{proposal.get('proposal_id') or 'unknown'} "
            f"status={proposal.get('proposal_status') or 'unknown'} "
            f"source={proposal.get('source_taskpack_id') or 'unknown'}"
        )
        if proposal.get("summary"):
            lines.append(f"summary: {proposal['summary']}")
    return "\n".join(lines) + "\n"


def _safe_artifact_id(value):
    safe = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in str(value or "")
    ).strip(".-")
    return safe or f"semantic-feedback-{int(datetime.now(UTC).timestamp())}"


def _bounded_text_items(values, limit):
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        return [_bounded_text(item, limit) for item in values if str(item or "").strip()]
    return [_bounded_text(values, limit)] if str(values or "").strip() else []


def _bounded_text(value, limit):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")

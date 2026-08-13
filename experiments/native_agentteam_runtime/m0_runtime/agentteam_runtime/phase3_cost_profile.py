"""Rebuild explainable Phase 3 cost projections from immutable run evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from .experiment_results import load_experiment_result_bundle


PROFILE_SCHEMA_VERSION = "phase3_cost_profile.v1"
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


class Phase3CostProfileError(RuntimeError):
    """Raised when retained Phase 3 cost evidence is inconsistent."""


def profile_phase3_pilot(pilot_root):
    """Project stage, mode, outcome, and wall-time costs for one pilot root."""

    root = Path(pilot_root).resolve(strict=True)
    state = _read_optional_json(root / "state" / "pilot-state.json") or {}
    schedule = {
        (
            item.get("instance_id"),
            item.get("mode"),
            item.get("repetition_index"),
        ): item.get("entry_id")
        for item in state.get("schedule", [])
        if isinstance(item, dict)
    }
    terminal_results = state.get("terminal_results", {})
    active_entry_id = (
        state.get("active", {}).get("entry_id")
        if isinstance(state.get("active"), dict)
        else None
    )
    runs = []
    for run_dir in sorted(root.glob("mode-runs/*/runs/*")):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        result_path = run_dir / "results" / "terminal" / "result.json"
        sealed = (
            load_experiment_result_bundle(run_dir)
            if result_path.is_file()
            else None
        )
        binding = _read_optional_json(run_dir / "binding.json")
        if sealed is None and not _has_invocation_reference(run_dir):
            continue
        identity = sealed["bundle"] if sealed is not None else binding
        if not isinstance(identity, dict):
            raise Phase3CostProfileError(
                "unsealed run identity is unavailable"
            )
        instance_id = run_dir.parents[1].name
        entry_id = schedule.get(
            (instance_id, identity["mode"], identity["repetition_index"])
        )
        pilot_result = (
            terminal_results.get(entry_id)
            if isinstance(terminal_results, dict) and entry_id
            else None
        )
        runs.append(
            _profile_mode_run(
                run_dir,
                sealed,
                instance_id=instance_id,
                entry_id=entry_id,
                pilot_result=pilot_result,
                binding=binding,
                controller_failed=(
                    state.get("status") == "stopped"
                    and entry_id == active_entry_id
                ),
            )
        )

    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "pilot_root": str(root),
        "run_count": len(runs),
        "complete_usage_run_count": sum(
            run["usage_coverage"]["status"] == "complete" for run in runs
        ),
        "reported_token_totals": _empty_tokens(),
        "wall_time_seconds": _empty_wall_time(),
        "by_mode": {},
        "by_stage": {},
        "by_role": {},
        "by_outcome": {},
        "evidence_gaps": sorted(
            {
                gap
                for run in runs
                for gap in run["evidence_gaps"]
            }
        ),
        "runs": runs,
    }
    for run in runs:
        _add_tokens(profile["reported_token_totals"], run["reported_token_totals"])
        _add_wall_time(profile["wall_time_seconds"], run["wall_time_seconds"])
        _add_bucket(profile["by_mode"], run["mode"], run)
        _add_bucket(profile["by_outcome"], run["cost_outcome"], run)
        for stage, stage_profile in run["by_stage"].items():
            bucket = profile["by_stage"].setdefault(stage, _empty_bucket())
            bucket["run_count"] += 1
            bucket["invocation_count"] += stage_profile["invocation_count"]
            _add_tokens(
                bucket["reported_token_totals"],
                stage_profile["reported_token_totals"],
            )
            bucket["wall_time_seconds"]["model_invocations"] += (
                stage_profile["wall_time_seconds"]
            )
            bucket["wall_time_seconds"]["total_observed"] += (
                stage_profile["wall_time_seconds"]
            )
        for role, role_profile in run["by_role"].items():
            bucket = profile["by_role"].setdefault(role, _empty_bucket())
            bucket["run_count"] += 1
            bucket["invocation_count"] += role_profile["invocation_count"]
            _add_tokens(
                bucket["reported_token_totals"],
                role_profile["reported_token_totals"],
            )
            bucket["wall_time_seconds"]["model_invocations"] += (
                role_profile["wall_time_seconds"]
            )
            bucket["wall_time_seconds"]["total_observed"] += (
                role_profile["wall_time_seconds"]
            )
    return profile


def render_phase3_cost_profile(profile):
    """Render a concise operator-facing view without hiding cached input."""

    if not isinstance(profile, dict) or profile.get("schema_version") != (
        PROFILE_SCHEMA_VERSION
    ):
        raise Phase3CostProfileError("Phase 3 cost profile is invalid")
    totals = profile["reported_token_totals"]
    wall = profile["wall_time_seconds"]
    lines = [
        (
            f"runs: {profile['run_count']} "
            f"usage_complete={profile['complete_usage_run_count']}/"
            f"{profile['run_count']}"
        ),
        (
            "tokens: "
            f"total={totals['total_tokens']} "
            f"input={totals['input_tokens']} "
            f"cached_input={totals['cached_input_tokens']} "
            f"uncached_input={totals['uncached_input_tokens']} "
            f"output={totals['output_tokens']} "
            f"reasoning={totals['reasoning_tokens']}"
        ),
        (
            "wall_seconds: "
            f"model={wall['model_invocations']:.3f} "
            f"orchestration_and_common_acceptance="
            f"{wall['orchestration_and_common_acceptance']:.3f} "
            f"official_evaluator={wall['official_evaluator']:.3f} "
            f"total={wall['total_observed']:.3f}"
        ),
    ]
    for label, key in (
        ("modes", "by_mode"),
        ("stages", "by_stage"),
        ("roles", "by_role"),
        ("outcomes", "by_outcome"),
    ):
        values = profile.get(key, {})
        if values:
            lines.append(
                f"{label}: "
                + ", ".join(
                    f"{name}={details['reported_token_totals']['total_tokens']}"
                    for name, details in sorted(values.items())
                )
            )
    if profile.get("evidence_gaps"):
        lines.append("evidence_gaps: " + ", ".join(profile["evidence_gaps"]))
    return "\n".join(lines)


def _profile_mode_run(
    run_dir,
    sealed,
    *,
    instance_id,
    entry_id,
    pilot_result,
    binding,
    controller_failed,
):
    bundle = sealed["bundle"] if sealed is not None else None
    reference = _find_invocation_reference(
        run_dir,
        (
            bundle["result_evidence"]["invocation_set_reference_sha256"]
            if bundle is not None
            else None
        ),
    )
    invocation_profile = _profile_invocations(reference)
    if (
        bundle is None
        and invocation_profile["usage_coverage"]["status"] != "complete"
    ):
        raise Phase3CostProfileError(
            "unsealed run requires complete invocation terminal usage"
        )
    if bundle is not None and (
        invocation_profile["reported_token_totals"]
        != _tokens_with_uncached(bundle["usage_totals"])
    ):
        raise Phase3CostProfileError(
            "invocation cost projection differs from sealed usage totals"
        )

    budget_wall = (
        float(
            bundle.get("budget_result", {}).get(
                "elapsed_wall_time_seconds", 0.0
            )
        )
        if bundle is not None
        else invocation_profile["model_wall_time_seconds"]
    )
    model_wall = invocation_profile["model_wall_time_seconds"]
    official_score = _read_optional_json(run_dir / "results" / "official-score.json")
    evaluator_failure = _read_optional_json(
        run_dir / "results" / "official-evaluator-failure.json"
    )
    official_wall = 0.0
    if isinstance(official_score, dict):
        official_wall = float(official_score.get("wall_time_seconds", 0.0))
    elif isinstance(evaluator_failure, dict):
        official_wall = float(evaluator_failure.get("wall_time_seconds", 0.0))
    infrastructure_failed = (
        bundle is None
        or controller_failed
        or bundle["terminal_status"] == "infrastructure_failed"
        or isinstance(evaluator_failure, dict)
        or (
            isinstance(pilot_result, dict)
            and pilot_result.get("failure_class")
            in {"provider_infrastructure_error", "evaluator_failure"}
        )
    )
    cost_outcome = (
        "infrastructure_waste"
        if infrastructure_failed
        else "scored_execution"
        if isinstance(official_score, dict)
        else "incomplete_execution"
    )
    evidence_gaps = []
    if bundle is None:
        evidence_gaps.append("sealed_result_missing")
    if controller_failed:
        evidence_gaps.append("pilot_controller_failed_after_provider_usage")
    if official_score is None and evaluator_failure is None:
        evidence_gaps.append("official_evaluator_terminal_evidence_missing")
    wall = {
        "model_invocations": model_wall,
        "orchestration_and_common_acceptance": max(budget_wall - model_wall, 0.0),
        "official_evaluator": official_wall,
        "total_observed": budget_wall + official_wall,
    }
    return {
        "experiment_run_id": (
            bundle["experiment_run_id"]
            if bundle is not None
            else binding["experiment_run_id"]
        ),
        "entry_id": entry_id,
        "instance_id": instance_id,
        "mode": bundle["mode"] if bundle is not None else binding["mode"],
        "repetition_index": (
            bundle["repetition_index"]
            if bundle is not None
            else binding["repetition_index"]
        ),
        "terminal_status": (
            bundle["terminal_status"] if bundle is not None else "unsealed"
        ),
        "cost_outcome": cost_outcome,
        "usage_coverage": copy.deepcopy(
            bundle["usage_coverage"]
            if bundle is not None
            else invocation_profile["usage_coverage"]
        ),
        "reported_token_totals": invocation_profile["reported_token_totals"],
        "wall_time_seconds": wall,
        "by_stage": invocation_profile["by_stage"],
        "by_role": invocation_profile["by_role"],
        "evidence_gaps": evidence_gaps,
    }


def _profile_invocations(reference_path):
    manifest = _read_json(reference_path, "model invocation set reference")
    if manifest.get("schema_version") != "experiment_model_invocation_manifest.v1":
        raise Phase3CostProfileError("model invocation set reference is invalid")
    totals = _empty_tokens()
    stages = {}
    roles = {}
    covered = 0
    count = 0
    model_wall = 0.0
    for invocation_set in manifest.get("invocation_sets", []):
        if not isinstance(invocation_set, dict):
            raise Phase3CostProfileError("model invocation set is invalid")
        lifecycle_root = Path(
            invocation_set.get("lifecycle_authority_root", "")
        ).resolve(strict=True)
        for invocation_id in invocation_set.get("invocation_ids", []):
            if not isinstance(invocation_id, str) or "/" in invocation_id:
                raise Phase3CostProfileError("model invocation ID is invalid")
            terminal = _read_json(
                lifecycle_root / "model_invocations" / invocation_id / "terminal.json",
                "model invocation terminal",
            )
            if terminal.get("invocation_id") != invocation_id:
                raise Phase3CostProfileError("model invocation identity changed")
            count += 1
            wall = float(terminal.get("wall_time_seconds", 0.0))
            if wall < 0:
                raise Phase3CostProfileError("model invocation wall time is invalid")
            model_wall += wall
            stage = terminal.get("usage_stage") or "unknown"
            stage_profile = stages.setdefault(
                stage,
                {
                    "invocation_count": 0,
                    "reported_invocation_count": 0,
                    "reported_token_totals": _empty_tokens(),
                    "wall_time_seconds": 0.0,
                    "terminal_status_counts": {},
                },
            )
            stage_profile["invocation_count"] += 1
            stage_profile["wall_time_seconds"] += wall
            role = terminal.get("role") or "unknown"
            role_profile = roles.setdefault(
                role,
                {
                    "invocation_count": 0,
                    "reported_invocation_count": 0,
                    "reported_token_totals": _empty_tokens(),
                    "wall_time_seconds": 0.0,
                    "terminal_status_counts": {},
                },
            )
            role_profile["invocation_count"] += 1
            role_profile["wall_time_seconds"] += wall
            terminal_status = terminal.get("terminal_status") or "unknown"
            stage_profile["terminal_status_counts"][terminal_status] = (
                stage_profile["terminal_status_counts"].get(terminal_status, 0) + 1
            )
            role_profile["terminal_status_counts"][terminal_status] = (
                role_profile["terminal_status_counts"].get(terminal_status, 0) + 1
            )
            if terminal.get("usage_status") != "reported":
                continue
            tokens = _tokens_with_uncached(terminal)
            covered += 1
            stage_profile["reported_invocation_count"] += 1
            role_profile["reported_invocation_count"] += 1
            _add_tokens(totals, tokens)
            _add_tokens(stage_profile["reported_token_totals"], tokens)
            _add_tokens(role_profile["reported_token_totals"], tokens)
    return {
        "usage_coverage": {
            "status": "complete" if covered == count else "partial" if covered else "unavailable",
            "covered_invocations": covered,
            "total_invocations": count,
        },
        "reported_token_totals": totals,
        "model_wall_time_seconds": model_wall,
        "by_stage": dict(sorted(stages.items())),
        "by_role": dict(sorted(roles.items())),
    }


def _find_invocation_reference(run_dir, expected_sha256=None):
    authority = Path(run_dir) / "authority" / "experiment_authority"
    matches = []
    for path in authority.glob("*.invocation-set.json"):
        if (
            not path.is_symlink()
            and path.is_file()
            and (
                expected_sha256 is None
                or _file_sha256(path) == expected_sha256
            )
        ):
            matches.append(path)
    if len(matches) != 1:
        raise Phase3CostProfileError(
            "exactly one bound model invocation set reference is required"
        )
    return matches[0]


def _has_invocation_reference(run_dir):
    authority = Path(run_dir) / "authority" / "experiment_authority"
    return any(
        not path.is_symlink() and path.is_file()
        for path in authority.glob("*.invocation-set.json")
    )


def _tokens_with_uncached(value):
    tokens = {}
    for field in _TOKEN_FIELDS:
        if field == "uncached_input_tokens":
            continue
        item = value.get(field, 0)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise Phase3CostProfileError(f"invalid token count: {field}")
        tokens[field] = item
    if tokens["cached_input_tokens"] > tokens["input_tokens"]:
        raise Phase3CostProfileError("cached input exceeds input tokens")
    tokens["uncached_input_tokens"] = (
        tokens["input_tokens"] - tokens["cached_input_tokens"]
    )
    return {field: tokens[field] for field in _TOKEN_FIELDS}


def _empty_tokens():
    return {field: 0 for field in _TOKEN_FIELDS}


def _empty_wall_time():
    return {
        "model_invocations": 0.0,
        "orchestration_and_common_acceptance": 0.0,
        "official_evaluator": 0.0,
        "total_observed": 0.0,
    }


def _empty_bucket():
    return {
        "run_count": 0,
        "invocation_count": 0,
        "reported_token_totals": _empty_tokens(),
        "wall_time_seconds": _empty_wall_time(),
    }


def _add_bucket(buckets, key, run):
    bucket = buckets.setdefault(key, _empty_bucket())
    bucket["run_count"] += 1
    bucket["invocation_count"] += sum(
        stage["invocation_count"] for stage in run["by_stage"].values()
    )
    _add_tokens(bucket["reported_token_totals"], run["reported_token_totals"])
    _add_wall_time(bucket["wall_time_seconds"], run["wall_time_seconds"])


def _add_tokens(target, source):
    for field in _TOKEN_FIELDS:
        target[field] += source[field]


def _add_wall_time(target, source):
    for field in target:
        target[field] += source[field]


def _read_optional_json(path):
    path = Path(path)
    if not path.exists():
        return None
    return _read_json(path, str(path))


def _read_json(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Phase3CostProfileError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Phase3CostProfileError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise Phase3CostProfileError(f"{label} is invalid")
    return value


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Profile retained Phase 3 costs")
    parser.add_argument("--pilot-root", required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    profile = profile_phase3_pilot(args.pilot_root)
    if args.as_json:
        print(json.dumps(profile, sort_keys=True, indent=2, ensure_ascii=True))
    else:
        print(render_phase3_cost_profile(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

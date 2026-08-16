"""Run the gated Phase 3 implementation-first worker experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .adaptive_worker_turn_experiment import (
    AdaptiveWorkerTurnExperimentRunner,
    profile_execution_observations,
)
from .m0_runtime import CodexRuntimeAdapter, run_integration_verification_additions
from .model_context_budget import TOOL_BUDGET_POLICY, codex_context_policy_arguments
from .phase3_worker_turn_live import _patch_id, _recover_stage_result


ENV_GATE = "AGENTTEAM_RUN_LIVE_ADAPTIVE_WORKER_EXPERIMENT"
STAGE_TOOL_LIMITS = {
    "implement": (28, 40),
    "repair": (16, 24),
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the gated Phase 3 implementation-first worker treatment."
    )
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo-map-handoff", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-profile", default="high")
    parser.add_argument("--maximum-total-tokens", type=int, default=600_000)
    parser.add_argument("--timeout-seconds", type=int, default=1_200)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get(ENV_GATE) != "1":
        _print_json({"status": "skipped", "reason": f"set {ENV_GATE}=1"})
        return 0
    try:
        result = run_live_adaptive_worker_experiment(
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            task_path=args.task,
            repo_map_handoff=args.repo_map_handoff,
            output_dir=args.output_dir,
            run_id=args.run_id,
            model=args.model,
            reasoning_profile=args.reasoning_profile,
            maximum_total_tokens=args.maximum_total_tokens,
            timeout_seconds=args.timeout_seconds,
            resume=args.resume,
        )
    except Exception as exc:  # pragma: no cover - live failure boundary
        _print_json({"status": "failed", "error": str(exc)[:1000]})
        return 1
    _print_json(result)
    return 0 if result["status"] in {"completed", "stopped"} else 1


def run_live_adaptive_worker_experiment(
    *,
    source_repository,
    source_commit,
    task_path,
    repo_map_handoff,
    output_dir,
    run_id,
    model,
    reasoning_profile,
    maximum_total_tokens,
    timeout_seconds,
    resume=False,
):
    output = Path(output_dir).resolve()
    if output.exists() and not resume:
        raise RuntimeError(f"refusing to reuse live experiment output: {output}")
    output.mkdir(parents=True, exist_ok=resume)
    worktree = output / "worktree"
    if not resume:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                str(Path(source_repository).resolve(strict=True)),
                str(worktree),
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "checkout", "--quiet", source_commit],
            check=True,
        )
    elif not worktree.is_dir():
        raise RuntimeError("live adaptive-worker resume worktree is missing")
    if _git(worktree, "rev-parse", "HEAD").strip() != source_commit:
        raise RuntimeError("live adaptive-worker source commit differs")

    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    handoff = json.loads(Path(repo_map_handoff).read_text(encoding="utf-8"))
    _validate_handoff_binding(task, handoff, source_commit)
    git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip())
    handoff_path = git_dir / "agentteam-worker-turns" / run_id / "repo-map-handoff.json"
    if not resume:
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(repo_map_handoff).resolve(strict=True), handoff_path)
    elif not handoff_path.is_file():
        raise RuntimeError("live adaptive-worker repo-map handoff is missing")
    task["input_artifacts"] = [str(handoff_path)]

    provider_wall_time_seconds = []
    controller_verification_wall_time_seconds = []
    stage_dirs = {
        stage: output / "provider" / stage
        for stage in STAGE_TOOL_LIMITS
        if (output / "provider" / stage).is_dir()
    }

    def invoke(stage, message, runtime_worktree):
        stage_output = output / "provider" / stage
        if stage_output.exists():
            if not resume:
                raise RuntimeError(f"provider stage output already exists: {stage}")
            print(f"[adaptive-worker] stage={stage} status=recovering", flush=True)
            result = _recover_stage_result(stage_output, message)
            provider_wall_time_seconds.append(_provider_wall_time(stage_output))
            return result
        stage_output.mkdir(parents=True, exist_ok=False)
        stage_dirs[stage] = stage_output
        soft_limit, hard_limit = STAGE_TOOL_LIMITS[stage]
        policy = {
            "tool_output_token_limit": 4000,
            "web_search_policy": "disabled",
            "model_auto_compact_token_limit": 32768,
            "tool_call_soft_limit": soft_limit,
            "tool_call_hard_limit": hard_limit,
            "tool_budget_policy": TOOL_BUDGET_POLICY,
        }
        adapter = CodexRuntimeAdapter(
            model=model,
            reasoning_profile=reasoning_profile,
            sandbox="workspace-write",
            timeout_seconds=timeout_seconds,
            extra_args=codex_context_policy_arguments(policy),
            output_dir=stage_output,
            progress_interval_seconds=30,
        )
        print(f"[adaptive-worker] stage={stage} status=started", flush=True)
        started = time.monotonic()
        result = adapter.run(message, worktree_path=runtime_worktree)
        elapsed = time.monotonic() - started
        provider_wall_time_seconds.append(elapsed)
        usage = result.get("token_usage", {})
        print(
            "[adaptive-worker] stage={} status={} total_tokens={}".format(
                stage,
                result.get("result_status"),
                usage.get("total_tokens", "unavailable"),
            ),
            flush=True,
        )
        return result

    def verify(additions, runtime_worktree):
        print("[adaptive-worker] controller_verification status=started", flush=True)
        started = time.monotonic()
        result = run_integration_verification_additions(additions, runtime_worktree)
        controller_verification_wall_time_seconds.append(time.monotonic() - started)
        print(
            "[adaptive-worker] controller_verification status={}".format(
                result.get("integration_verification_additions_status", "unknown")
            ),
            flush=True,
        )
        return result

    runner = AdaptiveWorkerTurnExperimentRunner(
        worktree_path=worktree,
        output_dir=output / "controller",
        run_id=run_id,
        task=task,
        invoke=invoke,
        run_verification=verify,
        maximum_total_tokens=maximum_total_tokens,
        allow_repair=True,
    )
    state = runner.run()
    patch_path = output / "candidate.patch"
    patch = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", "HEAD", "--"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    patch_path.write_bytes(patch)
    transcripts = []
    for stage in STAGE_TOOL_LIMITS:
        stage_dir = stage_dirs.get(stage)
        if stage_dir:
            transcripts.extend(sorted(stage_dir.glob("model_invocations/*/stdout.jsonl")))
    observations = profile_execution_observations(transcripts)
    observations_path = output / "execution-observations.json"
    observations_path.write_text(
        json.dumps(observations, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    final_worktree = _worktree_summary(worktree)
    report = {
        "schema_version": "phase3_adaptive_worker_live_result.v1",
        "status": state["status"],
        "stop_reason": state["stop_reason"],
        "run_id": run_id,
        "source_commit": source_commit,
        "model": model,
        "reasoning_profile": reasoning_profile,
        "maximum_total_tokens": maximum_total_tokens,
        "usage": state["usage"],
        "model_turns": state["model_turns"],
        "model_invocation_count": len(state["model_turns"]),
        "repair_invocations": state["repair_invocations"],
        "controller_verifications": state["controller_verifications"],
        "provider_wall_time_seconds": round(sum(provider_wall_time_seconds), 3),
        "controller_verification_wall_time_seconds": round(
            sum(controller_verification_wall_time_seconds), 3
        ),
        "checkpoint_count": sum(
            1 for item in state["model_turns"] if item["checkpoint_path"]
        ),
        "checkpoint_bytes": sum(item["checkpoint_bytes"] for item in state["model_turns"]),
        "candidate_patch_bytes": len(patch),
        "candidate_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "candidate_patch_id": _patch_id(patch_path),
        "changed_files": final_worktree["changed_files"],
        "worktree_state_sha256": final_worktree["state_sha256"],
        "execution_observations": observations,
        "execution_observations_path": str(observations_path),
        "worktree": str(worktree),
    }
    (output / "result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _validate_handoff_binding(task, handoff, source_commit):
    if handoff.get("task_id") != task.get("task_id"):
        raise RuntimeError("repository handoff task binding differs")
    repository = handoff.get("repository")
    handoff_commit = (
        repository.get("commit") if isinstance(repository, dict) else None
    )
    if handoff_commit != source_commit:
        raise RuntimeError("repository handoff source commit differs")
    objective = str(task.get("objective") or "")
    if handoff.get("objective_sha256") != hashlib.sha256(
        objective.encode("utf-8")
    ).hexdigest():
        raise RuntimeError("repository handoff objective binding differs")
    gaps = handoff.get("semantic_gaps", [])
    if not isinstance(gaps, list) or gaps:
        raise RuntimeError("repository handoff has unresolved semantic gaps")


def _provider_wall_time(stage_output):
    terminals = sorted(Path(stage_output).glob("model_invocations/*/terminal.json"))
    if len(terminals) != 1:
        return 0.0
    terminal = json.loads(terminals[0].read_text(encoding="utf-8"))
    value = terminal.get("wall_time_seconds")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.0


def _worktree_summary(worktree):
    from .worker_turn_checkpoint import worktree_state

    return worktree_state(worktree)


def _git(worktree, *arguments):
    return subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _print_json(value):
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())

"""Run one authorized live treatment for bounded worker turns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .m0_runtime import CodexRuntimeAdapter
from .model_context_budget import TOOL_BUDGET_POLICY, codex_context_policy_arguments
from .worker_turn_checkpoint import profile_worker_turn_transcripts
from .worker_turn_experiment import WorkerTurnExperimentRunner


ENV_GATE = "AGENTTEAM_RUN_LIVE_WORKER_TURN_EXPERIMENT"
STAGE_TOOL_LIMITS = {
    "locate": (8, 12),
    "implement": (16, 24),
    "verify": (8, 12),
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the gated Phase 3 bounded worker-turn treatment."
    )
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo-map-handoff")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-profile", default="high")
    parser.add_argument("--maximum-total-tokens", type=int, default=600_000)
    parser.add_argument("--timeout-seconds", type=int, default=1_200)
    args = parser.parse_args(argv)
    if os.environ.get(ENV_GATE) != "1":
        _print_json({"status": "skipped", "reason": f"set {ENV_GATE}=1"})
        return 0
    try:
        result = run_live_worker_turn_experiment(
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
        )
    except Exception as exc:  # pragma: no cover - live failure boundary
        _print_json({"status": "failed", "error": str(exc)[:1000]})
        return 1
    _print_json(result)
    return 0 if result["status"] in {"completed", "stopped"} else 1


def run_live_worker_turn_experiment(
    *,
    source_repository,
    source_commit,
    task_path,
    output_dir,
    run_id,
    model,
    reasoning_profile,
    maximum_total_tokens,
    timeout_seconds,
    repo_map_handoff=None,
):
    output = Path(output_dir).resolve()
    if output.exists():
        raise RuntimeError(f"refusing to reuse live experiment output: {output}")
    output.mkdir(parents=True)
    worktree = output / "worktree"
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
    actual_commit = _git(worktree, "rev-parse", "HEAD").strip()
    if actual_commit != source_commit:
        raise RuntimeError("live worker-turn source commit differs")
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    if repo_map_handoff:
        git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip())
        handoff_path = git_dir / "agentteam-worker-turns" / run_id / "repo-map-handoff.json"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(repo_map_handoff).resolve(strict=True), handoff_path)
        task["input_artifacts"] = [str(handoff_path)]

    stage_dirs = {}

    def invoke(stage, message, runtime_worktree):
        stage_output = output / "provider" / stage
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
        print(f"[worker-turn] stage={stage} status=started", flush=True)
        result = adapter.run(message, worktree_path=runtime_worktree)
        usage = result.get("token_usage", {})
        print(
            "[worker-turn] stage={} status={} total_tokens={}".format(
                stage,
                result.get("result_status"),
                usage.get("total_tokens", "unavailable"),
            ),
            flush=True,
        )
        return result

    runner = WorkerTurnExperimentRunner(
        worktree_path=worktree,
        output_dir=output / "controller",
        run_id=run_id,
        task=task,
        invoke=invoke,
        maximum_total_tokens=maximum_total_tokens,
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
    source_paths = sorted(
        {
            path
            for field in ("read_scope", "write_scope")
            for path in task.get(field, [])
            if isinstance(path, str) and not path.endswith("/")
        }
    )
    transcript_profile = profile_worker_turn_transcripts(transcripts, source_paths)
    report = {
        "schema_version": "phase3_worker_turn_live_result.v1",
        "status": state["status"],
        "stop_reason": state["stop_reason"],
        "run_id": run_id,
        "source_commit": source_commit,
        "model": model,
        "reasoning_profile": reasoning_profile,
        "maximum_total_tokens": maximum_total_tokens,
        "usage": state["usage"],
        "turns": state["turns"],
        "checkpoint_bytes": sum(item["checkpoint_bytes"] for item in state["turns"]),
        "candidate_patch_bytes": len(patch),
        "candidate_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "candidate_patch_id": _patch_id(patch_path),
        "changed_files": (
            state["turns"][-1]["changed_files"] if state["turns"] else []
        ),
        "transcript_profile": transcript_profile,
        "worktree": str(worktree),
    }
    (output / "result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _patch_id(path):
    if not path.stat().st_size:
        return None
    with path.open("rb") as handle:
        completed = subprocess.run(
            ["git", "patch-id", "--stable"],
            check=True,
            stdin=handle,
            stdout=subprocess.PIPE,
            text=False,
        )
    return completed.stdout.decode("ascii").split()[0]


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

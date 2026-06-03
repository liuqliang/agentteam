# M19 Two-Phase Integration Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect accepted two-phase worktree results to diff audit, patch artifact, integration apply, verification, and optional integration commit.

**Architecture:** Reuse the blocking scheduler integration helpers from `m0_runtime.py` inside `two_phase_scheduler.py`. Keep the feature side-by-side with existing blocking scheduler behavior and do not merge integration commits back to the source branch.

**Tech Stack:** Python 3.12 standard library, git worktrees, existing JSONL events, existing replay/SQLite state index, `unittest`.

---

### Task 1: Two-Phase Accepted Result Writes Patch And Commits Integration

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing API test**

Add test near existing two-phase tests:

```python
def test_two_phase_scheduler_can_commit_verified_integration_patch(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        _init_git_repo(repo)
        source_head = _git_rev_parse(repo, "HEAD")
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/"]),
            ],
        )
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            project_root=repo,
            integrate_accepted_patch=True,
            integration_verification_command=[
                sys.executable,
                "-c",
                "import pathlib; assert pathlib.Path('generated/two_phase_commit.json').exists()",
            ],
            commit_verified_integration=True,
        )

        scheduler.dispatch_ready()
        inflight = scheduler.state["inflight_attempts"][0]
        worktree_path = Path(inflight["worktree_path"])
        target = worktree_path / "generated" / "two_phase_commit.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"attempt_id": inflight["attempt_id"]}), encoding="utf-8")
        _append_runtime_result(
            inflight["outbox_path"],
            inflight["message_id"],
            inflight["task_id"],
            inflight["attempt_id"],
            inflight["lease_id"],
            "completed",
            ["generated/two_phase_commit.json"],
        )

        collected = scheduler.collect_ready_results()
        result = collected["results"][0]
        integration_worktree = Path(result["integration_worktree_path"])
        snapshot = replay_events(output_dir / "events.jsonl")

        self.assertEqual(result["validation_status"], "accepted")
        self.assertEqual(result["diff_audit"]["diff_status"], "matched")
        self.assertTrue(Path(result["patch_path"]).exists())
        self.assertEqual(result["integration_status"], "applied")
        self.assertEqual(result["integration_verification_status"], "passed")
        self.assertEqual(result["integration_commit_status"], "committed")
        self.assertNotEqual(result["integration_commit_sha"], None)
        self.assertTrue((integration_worktree / "generated" / "two_phase_commit.json").exists())
        self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
        self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
        self.assertEqual(
            snapshot["attempts"]["TASK-001-ATTEMPT-001"]["integration_commit_status"],
            "committed",
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_two_phase_scheduler_can_commit_verified_integration_patch \
  -v
```

Expected red: constructor rejects integration keyword arguments or collected
result lacks integration fields.

Observed red: `TwoPhaseFileScheduler.__init__()` rejected the unexpected
`integrate_accepted_patch` keyword.

- [x] **Step 3: Implement two-phase integration gate**

In `two_phase_scheduler.py`:

- import `audit_worktree_diff`, `write_patch_artifact`,
  `apply_patch_to_integration_worktree`, `run_integration_verification`, and
  `evaluate_integration_commit`;
- add constructor args `integrate_accepted_patch=False`,
  `integration_verification_command=None`, and
  `commit_verified_integration=False`;
- include default integration fields in every collected result;
- when `worktree_path` exists, compute `diff_audit` and `patch_path` before
  classification;
- classify with `classify_attempt_outcome(runtime_result, task, diff_audit=diff_audit)`;
- include `diff_audit` and `patch_path` in runtime output and validation events;
- for accepted attempts, append `patch_integrated`, `integration_verified`, and
  `integration_commit_evaluated` when their corresponding options are enabled;
- return collected `results` from `collect_ready_results()`.

- [x] **Step 4: Verify green**

Run the focused API test again. Expected: pass.

Observed green: focused API test passed; accepted two-phase worktree result
produced patch, integration verification, and committed integration worktree.

### Task 2: Two-Phase CLI Integration Flags

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI test**

Add test near the existing two-phase CLI test:

```python
def test_cli_two_phase_worker_pool_can_commit_verified_integration_patch(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        _init_git_repo(repo)
        source_head = _git_rev_parse(repo, "HEAD")
        backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.cli",
                "--agent-pool",
                str(agent_pool_path),
                "--backlog",
                str(backlog_path),
                "--output-dir",
                str(output_dir),
                "--project-root",
                str(repo),
                "--daemon-run-until-idle",
                "--daemon-two-phase-worker-pool",
                "--max-inflight",
                "1",
                "--integrate-accepted-patch",
                "--integration-verification-command-json",
                json.dumps([
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/m0_generated_repo_index.json').exists()",
                ]),
                "--commit-verified-integration",
            ],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        result = summary["steps"][0]["result"]
        integration_worktree = Path(result["integration_worktree_path"])

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(result["integration_status"], "applied")
        self.assertEqual(result["integration_verification_status"], "passed")
        self.assertEqual(result["integration_commit_status"], "committed")
        self.assertTrue((integration_worktree / "generated" / "m0_generated_repo_index.json").exists())
        self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_two_phase_worker_pool_can_commit_verified_integration_patch \
  -v
```

Expected red: CLI runs but summary result has `integration_status:
not_requested` because the two-phase branch does not pass integration options.

Observed red: CLI returned a result with `integration_worktree_path: null`,
causing the test to fail when converting it to `Path`.

- [x] **Step 3: Pass CLI integration options**

In `cli.py`, pass these existing parsed values into
`run_two_phase_scheduler_loop(...)`:

- `integrate_accepted_patch=args.integrate_accepted_patch`;
- `integration_verification_command=integration_verification_command`;
- `commit_verified_integration=args.commit_verified_integration`.

- [x] **Step 4: Verify green**

Run the focused CLI test again. Expected: pass.

Observed green: focused two-phase CLI integration test passed. Combined M19
API and CLI focused run passed 2 tests.

### Task 3: Docs, Full Verification, Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m19-two-phase-integration-gate.md`

- [x] **Step 1: Update docs**

Update `m0_file_runtime.md` with:

- M19 public API example for `TwoPhaseFileScheduler(..., integrate_accepted_patch=True, ...)`;
- CLI example using `--daemon-two-phase-worker-pool` with integration flags;
- M19 limits: no merge to source branch, no multi-task batch integration, no
  integration worktree cleanup.

Observed update: `m0_file_runtime.md` now documents the M19 two-phase API,
CLI integration flags, collected result fields, and remaining limits.

- [x] **Step 2: Full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m19-two-phase-integration-cli \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 2 \
  --max-attempts 2 \
  --lease-timeout-seconds 900
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
rg -n 'TB[D]|TO[D]O|implement[ ]later|fill[ ]in[ ]details|Similar[ ]to|approp[r]iate' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m19-two-phase-integration-gate.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m19-two-phase-integration-gate.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 3: Record observed verification**

Update this plan with exact observed pass or failure lines from the commands.

Observed pass:

- full unit test: `Ran 84 tests in 4.000s`, `OK`;
- artifact lint: `status: passed`, checked 21 JSON files and 1 JSONL file;
- two-phase worker-pool CLI smoke: `daemon_status: idle`,
  `scheduler_status: idle`, `processed_task_ids: ["TASK-001"]`;
- JSON validation: `find ... -name '*.json' -exec jq empty {} +` exited 0;
- sample JSONL validation: `jq -c . sample_events.jsonl` exited 0;
- bytecode compilation: `python3 -m compileall -q ...` exited 0;
- whitespace check: `git diff --check` exited 0;
- placeholder check: `rg` found no matches in the M19 design, M19 plan, or
  `m0_file_runtime.md`.

- [x] **Step 4: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m19-two-phase-integration-gate.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m19-two-phase-integration-gate.md
git commit -m "Add M19 two-phase integration gate"
git push origin native-runtime-m0
```

# M14a File Daemon Worker Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal file-backed daemon control-plane facade that keeps a persistent worker registry while delegating actual task execution to the existing sequential `FileScheduler`.

**Architecture:** M14a introduces `FileSchedulerDaemon` as a thin owner of daemon state and worker registry files. The daemon registers non-scheduler agents from `agent_pool`, refreshes heartbeat metadata on each tick, and calls `FileScheduler.step_once()` for exactly one scheduler step per tick. It does not add concurrent workers, long-lived Codex processes, process supervision, or a network API.

**Tech Stack:** Python 3.12 standard library, existing JSON state files, existing `FileScheduler`, `unittest`.

---

### Task 1: Daemon API and Worker Registry

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/daemon.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing daemon tick test**

Add imports:

```python
from agentteam_runtime import (
    FileSchedulerDaemon,
    run_file_daemon,
)
```

Add test:

```python
def test_file_daemon_tick_records_worker_registry_and_processes_one_task(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
            ],
        )

        daemon = FileSchedulerDaemon(
            FIXTURES / "sample_agent_pool.json",
            backlog_path,
            output_dir,
            clock=FixedClock(),
            runtime_adapter=FakeRuntimeAdapter(),
        )
        summary = daemon.tick()

        registry = json.loads(
            (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
        )

        self.assertEqual(summary["daemon_status"], "running")
        self.assertEqual(summary["tick_status"], "processed")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
        self.assertEqual(summary["worker_registry_path"], str(output_dir / "state" / "worker_registry.json"))
        self.assertEqual(registry["tick_count"], 1)
        self.assertEqual(registry["registry_status"], "active")
        self.assertEqual(
            [worker["agent_id"] for worker in registry["workers"]],
            ["agent-repo-map", "agent-worker-1"],
        )
        self.assertEqual(
            {worker["worker_status"] for worker in registry["workers"]},
            {"idle"},
        )
        self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_daemon_tick_records_worker_registry_and_processes_one_task \
  -v
```

Observed red: import failure because `FileSchedulerDaemon` is not exported yet.

- [x] **Step 3: Implement minimal daemon module**

Create `daemon.py` with:

```python
import json
from pathlib import Path

from .m0_runtime import FileScheduler, SystemClock


class FileSchedulerDaemon:
    def __init__(
        self,
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=None,
        project_root=None,
        runtime_adapter=None,
        runtime_adapter_factory=None,
        runtime_profile_defaults=None,
        max_attempts=1,
        cleanup_accepted_worktrees=False,
        integrate_accepted_patch=False,
        integration_verification_command=None,
        commit_verified_integration=False,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.backlog_path = backlog_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.worker_registry_path = self.output_dir / "state" / "worker_registry.json"
        self.scheduler = FileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=self.clock,
            project_root=project_root,
            runtime_adapter=runtime_adapter,
            runtime_adapter_factory=runtime_adapter_factory,
            runtime_profile_defaults=runtime_profile_defaults,
            max_attempts=max_attempts,
            cleanup_accepted_worktrees=cleanup_accepted_worktrees,
            integrate_accepted_patch=integrate_accepted_patch,
            integration_verification_command=integration_verification_command,
            commit_verified_integration=commit_verified_integration,
        )

    def tick(self):
        heartbeat_time = self.clock.now()
        registry = self._load_or_create_registry()
        registry["tick_count"] += 1
        registry["registry_status"] = "active"
        for worker in registry["workers"]:
            worker["worker_status"] = "idle"
            worker["active_task_id"] = None
            worker["last_heartbeat"] = heartbeat_time
        self._write_registry(registry)

        step = self.scheduler.step_once()
        scheduler_summary = self.scheduler._summary(self.scheduler.state["scheduler_status"])
        return {
            "daemon_status": "idle" if step["step_status"] == "idle" else "running",
            "tick_status": step["step_status"],
            "step": step,
            "processed_task_ids": scheduler_summary["processed_task_ids"],
            "step_count": scheduler_summary["step_count"],
            "events_path": scheduler_summary["events_path"],
            "state_path": scheduler_summary["state_path"],
            "state_db_path": scheduler_summary["state_db_path"],
            "worker_registry_path": str(self.worker_registry_path),
            "tick_count": registry["tick_count"],
        }

    def _load_or_create_registry(self):
        if self.worker_registry_path.exists():
            return json.loads(self.worker_registry_path.read_text(encoding="utf-8"))
        agent_pool = json.loads(self.agent_pool_path.read_text(encoding="utf-8"))
        workers = []
        for agent in agent_pool.get("agents", []):
            if agent.get("agent_id") == agent_pool.get("scheduler_agent_id"):
                continue
            workers.append(
                {
                    "worker_id": f"WORKER-{agent['agent_id']}",
                    "agent_id": agent["agent_id"],
                    "role": agent["role"],
                    "worker_status": "idle",
                    "runtime_adapter": agent.get("runtime_adapter"),
                    "runtime_profile": agent.get("runtime_profile"),
                    "active_task_id": None,
                    "last_heartbeat": None,
                }
            )
        return {
            "registry_status": "initialized",
            "tick_count": 0,
            "workers": workers,
        }

    def _write_registry(self, registry):
        self.worker_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_registry_path.write_text(json.dumps(registry, sort_keys=True), encoding="utf-8")


def run_file_daemon(*args, max_ticks=100, **kwargs):
    daemon = FileSchedulerDaemon(*args, **kwargs)
    return daemon.run_until_idle(max_ticks=max_ticks)
```

Export `FileSchedulerDaemon` and `run_file_daemon` from `__init__.py`.

- [x] **Step 4: Add run loop behavior**

Add test:

```python
def test_file_daemon_run_until_idle_reuses_worker_registry_across_ticks(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
            ],
        )

        summary = run_file_daemon(
            FIXTURES / "sample_agent_pool.json",
            backlog_path,
            output_dir,
            clock=FixedClock(),
            runtime_adapter=FakeRuntimeAdapter(),
        )
        registry = json.loads(
            (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
        )

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertEqual(summary["step_count"], 2)
        self.assertEqual(summary["tick_count"], 3)
        self.assertEqual(registry["tick_count"], 3)
        self.assertEqual(registry["registry_status"], "active")
```

Implement `FileSchedulerDaemon.run_until_idle(max_ticks=100)`: call `tick()` until
`tick_status == "idle"` or the tick budget is exhausted. Return a summary with
`daemon_status`, `processed_task_ids`, `step_count`, `tick_count`, and state paths.

- [x] **Step 5: Verify daemon tests green**

Run both focused daemon tests and then the full suite:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_daemon_tick_records_worker_registry_and_processes_one_task \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_daemon_run_until_idle_reuses_worker_registry_across_ticks \
  -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
```

Observed focused green: both daemon API tests passed. Full suite is recorded in Task 2 verification.

### Task 2: CLI Surface and Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m14a-file-daemon-worker-registry.md`

- [x] **Step 1: Add failing CLI daemon test**

Add test:

```python
def test_cli_can_run_file_daemon_until_idle(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
            ],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.cli",
                "--agent-pool",
                str(FIXTURES / "sample_agent_pool.json"),
                "--backlog",
                str(backlog_path),
                "--output-dir",
                str(output_dir),
                "--daemon-run-until-idle",
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertTrue((output_dir / "state" / "worker_registry.json").exists())
```

- [x] **Step 2: Verify red**

Observed red: CLI rejected unknown `--daemon-run-until-idle` with exit 2.

- [x] **Step 3: Add CLI flag**

In `cli.py`, import `run_file_daemon`, add `--daemon-run-until-idle`, and route
to `run_file_daemon(...)` with the same project/runtime/integration options as
`--run-until-idle`. Print the daemon summary as sorted JSON.

- [x] **Step 4: Update artifact docs**

Document:

- daemon state file: `<output-dir>/state/worker_registry.json`;
- daemon tick behavior: heartbeat + one `FileScheduler.step_once()`;
- current limitation: sequential only, no process supervision, no long-lived
  Codex/Claude session;
- when to use `--daemon-run-until-idle` versus `--run-until-idle`.

- [x] **Step 5: Full verification**

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
  --output-dir /tmp/agentteam-m14a-daemon-cli \
  --daemon-run-until-idle
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 64 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.cli ... --daemon-run-until-idle ... exit 0
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

- [x] **Step 6: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/daemon.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m14a-file-daemon-worker-registry.md
git commit -m "Add M14a file daemon worker registry"
git push origin native-runtime-m0
```

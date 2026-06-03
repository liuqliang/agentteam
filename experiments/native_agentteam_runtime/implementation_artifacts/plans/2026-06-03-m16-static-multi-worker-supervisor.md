# M16 Static Multi-Worker Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Start and stop one long-running mailbox worker process per selected agent in the agent pool while keeping scheduler execution sequential.

**Architecture:** Add a focused `worker_pool.py` module that composes existing `FileMailboxWorkerProcessSupervisor` instances. The daemon CLI gains `--daemon-long-running-worker-pool`, uses `FileMailboxExternalRuntimeAdapter` for scheduler execution, and includes worker pool start/stop summaries in the daemon output. The existing scheduler remains blocking and sequential.

**Tech Stack:** Python 3.12 standard library, existing JSONL mailboxes, existing worker process supervisor, `unittest`.

---

### Task 1: Worker Pool Supervisor API

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing worker pool API test**

Add imports:

```python
from agentteam_runtime import FileMailboxWorkerPoolSupervisor
```

Add helper near existing agent pool helpers:

```python
def _write_agent_pool_with_agent_ids(path, agent_ids):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent" if index == 0 else f"aux_role_{index}",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for index, agent_id in enumerate(agent_ids)
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")
```

Add test:

```python
def test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        _write_agent_pool_with_agent_ids(
            agent_pool_path,
            ["agent-repo-map", "agent-doc-map"],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")
        pool = FileMailboxWorkerPoolSupervisor(
            agent_pool_path,
            output_dir,
            env=env,
            poll_interval_seconds=0.01,
        )

        start = pool.start()
        try:
            self.assertEqual(start["pool_status"], "running")
            self.assertEqual(start["worker_count"], 2)
            self.assertEqual(
                {worker["worker_agent_id"] for worker in start["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(Path(start["process_registry_path"]).exists())
            self.assertTrue(all(worker["worker_pid"] != os.getpid() for worker in start["workers"]))
        finally:
            stop = pool.stop()

        registry = json.loads(Path(stop["process_registry_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stop["pool_status"], "stopped")
        self.assertEqual(stop["worker_count"], 2)
        self.assertEqual(registry["registry_status"], "stopped")
        self.assertEqual(
            {worker["worker_agent_id"] for worker in stop["workers"]},
            {"agent-repo-map", "agent-doc-map"},
        )
        self.assertTrue(all(worker["worker_status"] == "stopped" for worker in stop["workers"]))
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents \
  -v
```

Expected red: import failure because `FileMailboxWorkerPoolSupervisor` is not
implemented.

Observed red: import failure because `FileMailboxWorkerPoolSupervisor` is not
exported from `agentteam_runtime`.

- [x] **Step 3: Implement worker pool supervisor**

Create `worker_pool.py` with:

```python
import json
from pathlib import Path

from .mailbox_worker import FileMailboxWorkerProcessSupervisor


class FileMailboxWorkerPoolSupervisor:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        runtime_profile_defaults=None,
        env=None,
        poll_interval_seconds=0.05,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.runtime_profile_defaults = runtime_profile_defaults
        self.env = env
        self.poll_interval_seconds = poll_interval_seconds
        self.process_registry_path = self.output_dir / "state" / "worker_process_registry.json"
        self.workers = []

    def start(self):
        self.workers = [
            self._worker_for_agent(agent)
            for agent in _worker_agents(self.agent_pool_path)
        ]
        starts = [worker.start() for worker in self.workers]
        summary = self._summary("running", starts)
        self._write_registry(summary)
        return summary

    def stop(self):
        stops = [worker.stop() for worker in self.workers]
        summary = self._summary("stopped", stops)
        self._write_registry(summary)
        return summary

    def _worker_for_agent(self, agent):
        profile = self.runtime_profile_defaults or agent.get("runtime_profile") or {"adapter": "fake"}
        runtime = profile.get("adapter", "fake")
        if runtime not in {"fake", "codex"}:
            raise ValueError(f"unsupported mailbox worker pool runtime: {runtime}")
        return FileMailboxWorkerProcessSupervisor(
            self.agent_pool_path,
            self.output_dir,
            agent["agent_id"],
            env=self.env,
            poll_interval_seconds=self.poll_interval_seconds,
            runtime=runtime,
            codex_command=profile.get("command"),
            codex_model=profile.get("model"),
            codex_sandbox=profile.get("sandbox", "workspace-write"),
            codex_timeout_seconds=profile.get("timeout_seconds", 300),
        )

    def _summary(self, status, workers):
        return {
            "pool_status": status,
            "worker_count": len(workers),
            "process_registry_path": str(self.process_registry_path),
            "workers": workers,
        }

    def _write_registry(self, summary):
        self.process_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.process_registry_path.write_text(
            json.dumps(
                {
                    "registry_status": summary["pool_status"],
                    "worker_count": summary["worker_count"],
                    "workers": summary["workers"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _worker_agents(agent_pool_path):
    agent_pool = json.loads(Path(agent_pool_path).read_text(encoding="utf-8"))
    scheduler_agent_id = agent_pool.get("scheduler_agent_id")
    return [
        agent
        for agent in agent_pool.get("agents", [])
        if agent.get("agent_id") != scheduler_agent_id
    ]
```

Export `FileMailboxWorkerPoolSupervisor` from `__init__.py`.

- [x] **Step 4: Verify green**

Run the focused worker pool API test again. Expected: pass.

Observed green: focused worker pool supervisor API test passed.

### Task 2: Daemon CLI Worker Pool

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m16-static-multi-worker-supervisor.md`

- [x] **Step 1: Write failing daemon CLI worker pool test**

Add test near long-running worker daemon tests:

```python
def test_cli_can_run_file_daemon_with_static_long_running_worker_pool(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
        _write_agent_pool_with_agent_ids(
            agent_pool_path,
            ["agent-repo-map", "agent-doc-map"],
        )
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
                "--daemon-run-until-idle",
                "--daemon-long-running-worker-pool",
            ],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        process_registry = json.loads(
            Path(summary["worker_pool"]["process_registry_path"]).read_text(encoding="utf-8")
        )
        repo_outbox = (
            output_dir
            / "steps"
            / "STEP-0001-TASK-001"
            / "mailboxes"
            / "agent-repo-map"
            / "outbox.jsonl"
        )

        self.assertEqual(completed.stderr, "")
        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
        self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
        self.assertEqual(summary["worker_pool"]["worker_count"], 2)
        self.assertEqual(process_registry["registry_status"], "stopped")
        self.assertEqual(
            {worker["worker_agent_id"] for worker in summary["worker_pool"]["workers"]},
            {"agent-repo-map", "agent-doc-map"},
        )
        self.assertTrue(repo_outbox.exists())
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_can_run_file_daemon_with_static_long_running_worker_pool \
  -v
```

Expected red: CLI rejects unknown `--daemon-long-running-worker-pool`.

Observed red: CLI rejected `--daemon-long-running-worker-pool` as an
unrecognized argument.

- [x] **Step 3: Wire daemon CLI**

In `cli.py`:

- import `FileMailboxWorkerPoolSupervisor`;
- add `--daemon-long-running-worker-pool`;
- require `--daemon-run-until-idle`;
- make it mutually exclusive with `--daemon-mailbox-worker`,
  `--daemon-mailbox-subprocess-worker`, and
  `--daemon-long-running-mailbox-worker`;
- in the daemon branch, start `FileMailboxWorkerPoolSupervisor`;
- use `FileMailboxExternalRuntimeAdapter(args.agent_pool)` as scheduler runtime
  adapter;
- stop the pool in `finally`;
- attach `worker_pool` to the printed daemon result.

- [x] **Step 4: Verify green**

Run the focused daemon CLI worker pool test. Expected: pass.

Observed green: focused daemon CLI static worker pool test passed.

- [x] **Step 5: Update docs**

Update `m0_file_runtime.md` with:

- public API import for `FileMailboxWorkerPoolSupervisor`;
- CLI example for `--daemon-long-running-worker-pool`;
- M16 section explaining static pool, process registry, and sequential scheduler
  limitation;
- milestone summary line for M16.

Updated `m0_file_runtime.md` with the public API import, worker pool CLI
example, M16 section, process registry details, and sequential scheduler
limitation.

- [x] **Step 6: Full verification**

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
  --output-dir /tmp/agentteam-m16-static-worker-pool-cli \
  --daemon-run-until-idle \
  --daemon-long-running-worker-pool
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed pass:

- unittest discover: 77 tests passed;
- artifact lint: passed, 21 JSON files and 1 JSONL file checked;
- static worker pool CLI smoke: daemon idle, sample task processed, two workers
  stopped through stop files, `worker_process_registry.json` written;
- JSON/JQ checks: passed;
- compileall: passed;
- git diff check: passed.

- [x] **Step 7: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m16-static-multi-worker-supervisor.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m16-static-multi-worker-supervisor.md
git commit -m "Add M16 static multi-worker supervisor"
git push origin native-runtime-m0
```

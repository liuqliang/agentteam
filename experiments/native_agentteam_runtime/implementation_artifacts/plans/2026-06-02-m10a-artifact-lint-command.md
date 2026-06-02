# M10a Artifact Lint Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an executable native-runtime artifact lint command using only Python standard library dependencies.

**Architecture:** Add a small `agentteam_runtime.artifact_lint` module that scans a root directory for JSON and JSONL files, parses every file, and performs a basic event-schema enum check for event JSONL records. The command reports a JSON summary with status, checked counts, and structured errors.

**Tech Stack:** Python 3.12 standard library, `argparse`, `json`, `unittest`.

---

### Task 1: Artifact Lint API

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_lint.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing API tests**

Add tests for:

```python
from agentteam_runtime.artifact_lint import lint_artifacts

summary = lint_artifacts(ROOT)
self.assertEqual(summary["status"], "passed")
self.assertGreaterEqual(summary["checked_json_files"], 1)
self.assertGreaterEqual(summary["checked_jsonl_files"], 1)
self.assertEqual(summary["errors"], [])
```

And invalid JSON:

```python
bad_path.write_text("{bad", encoding="utf-8")
summary = lint_artifacts(tmp_path)
self.assertEqual(summary["status"], "failed")
self.assertEqual(summary["errors"][0]["kind"], "invalid_json")
```

- [x] **Step 2: Verify red**

Run the focused API tests. Expected: fail because `agentteam_runtime.artifact_lint` does not exist.

Observed red:

```text
ModuleNotFoundError: No module named 'agentteam_runtime.artifact_lint'
```

- [x] **Step 3: Implement API**

Implement:

```python
def lint_artifacts(root_path):
    ...
```

Return:

```json
{
  "status": "passed | failed",
  "root_path": "<path>",
  "checked_json_files": 0,
  "checked_jsonl_files": 0,
  "errors": []
}
```

For event JSONL records, load `schemas/event.schema.json` under the root when it exists and reject event types outside `properties.event_type.enum`.

- [x] **Step 4: Verify green**

Run the focused API tests. Expected: pass.

Observed green:

```text
test_artifact_lint_passes_native_runtime_tree ... ok
test_artifact_lint_reports_invalid_json ... ok
test_artifact_lint_reports_invalid_event_type ... ok
```

### Task 2: Executable Lint Command

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_lint.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI test**

Add a subprocess test:

```bash
python3 -m agentteam_runtime.artifact_lint --root <native-runtime-root>
```

Assert stdout JSON has:

```python
self.assertEqual(summary["status"], "passed")
self.assertEqual(completed.returncode, 0)
```

- [x] **Step 2: Verify red**

Run the focused CLI test. Expected: fail before the module has a `main(...)` entrypoint.

Observed red:

```text
python3 -m agentteam_runtime.artifact_lint ... returned non-zero exit status 1
```

- [x] **Step 3: Implement CLI**

Add:

```python
def main(argv=None):
    ...

if __name__ == "__main__":
    raise SystemExit(main())
```

Exit code is `0` for passed and `1` for failed.

- [x] **Step 4: Verify green**

Run the focused CLI test. Expected: pass.

Observed green:

```text
test_artifact_lint_cli_prints_summary ... ok
```

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m10a-artifact-lint-command.md`

- [x] **Step 1: Document M10a**

Document the command:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint \
  --root experiments/native_agentteam_runtime
```

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m10a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m10a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 47 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

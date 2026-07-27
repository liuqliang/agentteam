import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from agentteam_runtime.experiment_budget import (
    ExperimentBudgetController,
    ExperimentBudgetError,
    ExperimentBudgetIntegrityError,
    advance_experiment_budget,
    create_experiment_budget_state,
    validate_experiment_budget_state,
)
from agentteam_runtime.experiment_controller import (
    ExperimentControllerError,
    ExperimentControllerIntegrityError,
    ExperimentProviderAdmissionDenied,
    create_experiment_controller,
)
from agentteam_runtime.experiment_contract import (
    LEGACY_MANIFEST_SCHEMA_VERSION,
    ExperimentContractError,
    ExperimentLeaseError,
    acquire_controller_lease,
    allocate_experiment_run,
    build_experiment_run_manifest,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_experiment_run_id,
    ensure_executable_manifest,
    validate_experiment_protocol,
    validate_experiment_run_binding,
    validate_experiment_run_manifest,
    validate_experiment_state,
    validate_resume_binding,
)
from agentteam_runtime.experiment_workspace import (
    ExperimentWorkspaceError,
    allocate_clean_snapshot,
    cleanup_clean_snapshot,
    load_clean_snapshot_attestation,
    validate_clean_snapshot_attestation,
    verify_clean_snapshot,
)
from agentteam_runtime.token_usage import usage_event_id_for_invocation
from agentteam_runtime.experiment_sandbox import (
    _capture_bounded_process,
    _candidate_repository_state,
    _approved_acceptance_executable,
    _read_digest_bound_evaluator,
    _run_bounded_argv,
    _sandbox_policy_sha256,
    ExperimentEvaluationBlocked,
    ExperimentSandboxError,
    ExperimentSandboxUnavailable,
    _attach_namespace_evidence,
    build_provider_sandbox_descriptor,
    experiment_lifecycle_authority_root,
    prepare_candidate_evaluation_launch,
    prepare_provider_launch,
    probe_gold_canary_denial,
    publish_evaluator_reference,
    publish_experiment_protocol_reference,
    publish_model_invocation_set_reference,
    publish_provider_sandbox_reference,
    publish_scan_scope_reference,
    run_trusted_argv_evaluator,
    scan_canary_leakage,
    scan_scope_sha256,
    validate_evaluation_evidence,
    validate_provider_sandbox_descriptor,
)
from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    InvocationLifecycle,
    ModelInvocationCall,
    ModelInvocationIntegrityError,
    ModelInvocationUnavailable,
    ProviderExecution,
    invocation_context_from_message,
)
from agentteam_runtime.mailbox_worker import _model_invocation_context_payload
import agentteam_runtime.two_phase_scheduler as two_phase_scheduler_module
from agentteam_runtime.two_phase_scheduler import (
    TwoPhaseFileScheduler,
    reconcile_orphaned_invocation,
)


def _protocol():
    return {
        "schema_version": "experiment_protocol.v1",
        "experiment_id": "phase2-calibration",
        "instance_id": "fixture-001",
        "repository": {
            "source": "/srv/source/repository.git",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "git_object_format": "sha1",
        },
        "goal": {
            "summary": "Implement the deterministic fixture.",
            "constraints": [
                "Do not read evaluator-only state.",
                "Preserve the declared acceptance command.",
            ],
        },
        "acceptance": {
            "command": [
                str(Path(sys.executable).resolve()),
                "-m",
                "unittest",
                "tests.test_fixture",
            ],
            "timeout_seconds": 120,
        },
        "modes": [
            "single_codex",
            "agentteam_direct",
            "agentteam_full",
        ],
        "mode_order": [
            "agentteam_direct",
            "agentteam_full",
            "single_codex",
        ],
        "mode_instructions": {
            "single_codex": [],
            "agentteam_direct": ["Use only the preregistered taskpack."],
            "agentteam_full": ["Author and freeze the taskpack normally."],
        },
        "repetition_policy": {
            "count": 2,
            "order_strategy": "seeded_counterbalanced_rotation",
        },
        "environment": {
            "backend": "codex",
            "codex_cli_version": "codex-test-v1",
            "model": "codex-test-model",
            "reasoning_profile": "high",
            "service_configuration_sha256": "3" * 64,
            "sandbox_policy": "workspace-write",
            "permission_policy": "never",
            "network_policy": "disabled",
            "tool_allowlist": ["exec_command", "apply_patch"],
            "host_class": "deterministic-test",
            "cpu_limit": 2,
            "memory_limit_bytes": 1073741824,
            "dependency_cache_policy": "read_only_preregistered",
            "max_inflight_model_invocations": 1,
        },
        "seed": 17,
        "scored": False,
        "blind_gold": {
            "policy": "unavailable_to_runtime",
        },
        "budgets": {
            "max_total_tokens": 100000,
            "max_wall_time_seconds": 1800,
            "soft_warning_ratio": 0.8,
            "stop_boundaries": [
                "pre_provider_launch",
                "post_invocation_terminal",
                "pre_integration",
                "post_integration",
            ],
        },
        "operator_limits": {
            "expected_operator_action": 2,
            "corrective_intervention": 1,
            "decision_escalation": 1,
        },
        "evaluator": {
            "version": "fixture-evaluator.v1",
            "artifact_sha256": "4" * 64,
        },
        "direct_taskpack": {
            "sha256": "5" * 64,
            "cost_reported_separately": True,
            "available_to_full_mode": False,
        },
        "usage_contract_version": "model_invocation_usage.v1",
    }


def _release():
    return {
        "release_id": "candidate-v1",
        "release_root": "/srv/agentteam/releases/candidate-v1",
        "runtime_root": "/srv/agentteam/releases/candidate-v1/m0_runtime",
        "release_manifest_sha256": "6" * 64,
        "source_commit": "7" * 40,
        "git_object_format": "sha1",
    }


def _allocate(root, key="request-001"):
    return allocate_experiment_run(
        root,
        _protocol(),
        mode="single_codex",
        repetition_index=0,
        stable_request_key=key,
        runtime_release=_release(),
        bound_at="2026-07-27T00:00:00Z",
    )


def _git(repository, *arguments, check=True):
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed


def _fixture_repository(
    root,
    *,
    escaping_symlink=False,
    object_format="sha1",
):
    source = Path(root) / "source"
    source.mkdir()
    subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            f"--object-format={object_format}",
            str(source),
        ],
        check=True,
    )
    _git(source, "config", "user.name", "Experiment Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    (source / "history.txt").write_text("first\n", encoding="utf-8")
    _git(source, "add", "history.txt")
    _git(source, "commit", "--quiet", "-m", "first")
    parent_commit = _git(source, "rev-parse", "HEAD").stdout.strip()

    (source / "history.txt").write_text("second\n", encoding="utf-8")
    (source / "tracked.txt").write_text("clean fixture\n", encoding="utf-8")
    if escaping_symlink:
        os.symlink("../outside-secret", source / "escape")
    _git(source, "add", "-A")
    _git(source, "commit", "--quiet", "-m", "protocol source")
    commit = _git(source, "rev-parse", "HEAD").stdout.strip()
    tree = _git(source, "rev-parse", "HEAD^{tree}").stdout.strip()
    object_format = _git(
        source,
        "rev-parse",
        "--show-object-format",
    ).stdout.strip()

    _git(source, "branch", "source-only-branch", parent_commit)
    _git(source, "tag", "source-only-tag", parent_commit)
    _git(source, "remote", "add", "source-only-remote", str(source))
    prior_state = source / ".agentteam"
    prior_state.mkdir()
    (prior_state / "prior-run.json").write_text("{}\n", encoding="utf-8")
    (source / "untracked.patch").write_text("prior patch\n", encoding="utf-8")
    return {
        "repository": {
            "source": str(source),
            "commit": commit,
            "tree": tree,
            "git_object_format": object_format,
        },
        "parent_commit": parent_commit,
        "source": source,
    }


def _sandbox_fixture(root):
    root = Path(root)
    repository = root / "sandbox-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    _git(repository, "config", "user.name", "Sandbox Fixture")
    _git(repository, "config", "user.email", "sandbox@example.invalid")
    (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "sandbox fixture")
    repository_identity = {
        "commit": _git(repository, "rev-parse", "HEAD").stdout.strip(),
        "tree": _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip(),
        "git_object_format": _git(
            repository,
            "rev-parse",
            "--show-object-format",
        ).stdout.strip(),
    }
    credential = root / "provider-credential.json"
    credential.write_text('{"token":"bounded-test-token"}\n', encoding="utf-8")
    canary = root / "evaluator-only" / "gold-canary"
    canary.parent.mkdir()
    canary.write_bytes(b"agentteam-evaluator-only-canary")
    bwrap = Path("/usr/bin/bwrap")
    runtime_views = [
        {"source": path, "target": path}
        for path in ("/usr", "/lib", "/lib64", "/bin")
        if Path(path).exists()
    ]
    descriptor = build_provider_sandbox_descriptor(
        repository,
        runtime_views=runtime_views,
        credential_mounts=[
            {
                "source": str(credential),
                "target": "/run/agentteam-credentials/provider.json",
            }
        ],
        environment={
            "AGENTTEAM_CREDENTIAL_FILE": (
                "/run/agentteam-credentials/provider.json"
            )
        },
        bwrap_path=bwrap,
        repository_identity=repository_identity,
        forbidden_paths=[canary],
    )
    evidence = {
        "schema_version": "experiment_namespace_probe.v1",
        "evidence_status": "complete",
        "denial_status": "denied",
        "policy_sha256": descriptor["policy_sha256"],
        "canary_sha256": hashlib.sha256(canary.read_bytes()).hexdigest(),
        "path_visible": False,
        "content_readable": False,
        "probe_returncode": 0,
    }
    return {
        "repository": repository,
        "repository_identity": repository_identity,
        "credential": credential,
        "canary": canary,
        "evidence": evidence,
        "descriptor": _attach_namespace_evidence(descriptor, evidence),
        "uncertified_descriptor": descriptor,
    }


def _publish_test_sandbox_reference(authority_root, fixture):
    with patch(
        "agentteam_runtime.experiment_sandbox.probe_gold_canary_denial",
        return_value=fixture["evidence"],
    ):
        return publish_provider_sandbox_reference(
            authority_root,
            fixture["uncertified_descriptor"],
            fixture["canary"],
        )


def _sandbox_protocol(fixture):
    protocol = _protocol()
    protocol["repository"] = {
        "source": str(fixture["repository"]),
        **fixture["repository_identity"],
    }
    return protocol


def _model_context(*, supported, sandbox_reference):
    context = {
        "project": "experiment-fixture",
        "run_id": "RUN-EXPERIMENT-FIXTURE",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase2-fixture",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "P2-02B",
        "attempt_id": "ATTEMPT-P2-02B",
        "runtime_execution_session_id": "SESSION-P2-02B",
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": "OWNER-P2-02B",
        "agent_id": "agent-fixture",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": "fixture-model",
        "coverage_class": (
            "supported_model_invocation"
            if supported
            else "not_applicable_adapter"
        ),
        "experiment_sandbox_reference": sandbox_reference,
    }
    context["_explicit_context_fields"] = {
        field: True
        for field in (
            "project",
            "run_id",
            "taskpack_id",
            "runtime_execution_session_id",
            "lifecycle_owner_token",
            "agent_id",
            "role",
            "usage_stage",
        )
    }
    return context


def _test_evaluator_execution(
    argv,
    *,
    cwd,
    environment,
    timeout_seconds,
    max_output_bytes,
    cpu_limit,
    memory_limit_bytes,
    input_bytes=None,
):
    del cpu_limit, memory_limit_bytes
    result = _capture_bounded_process(
        argv,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        input_bytes=input_bytes,
    )
    result.update(
        {
            "execution_boundary": "systemd_user_transient_service",
            "systemd_unit": "agentteam-eval-" + "a" * 24 + ".service",
        }
    )
    return result


def _budget_usage(
    suffix,
    *,
    input_tokens,
    output_tokens,
    cached_input_tokens=0,
    reasoning_tokens=0,
    usage_status="reported",
    unavailable_reason=None,
):
    invocation_id = f"INV-{suffix}"
    start_sha256 = hashlib.sha256(
        _authority_record_bytes({"invocation_id": invocation_id})
    ).hexdigest()
    return {
        "usage_schema_version": "model_invocation_usage.v1",
        "usage_event_id": usage_event_id_for_invocation(invocation_id),
        "invocation_id": invocation_id,
        "start_sha256": start_sha256,
        "project": "agentteam",
        "run_id": "phase2-budget-fixture",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase2-budget-fixture",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "P2-03A",
        "attempt_id": f"ATTEMPT-{suffix}",
        "runtime_execution_session_id": f"SESSION-{suffix}",
        "provider_session_id": None,
        "provider_predecessor_invocation_id": None,
        "provider_turn_id": None,
        "provider_predecessor_turn_id": None,
        "lifecycle_owner_token": f"LEASE-{suffix}",
        "terminal_writer": "worker",
        "agent_id": "agent-implementation-worker-1",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": None,
        "coverage_class": "supported_model_invocation",
        "terminal_status": "completed",
        "usage_status": usage_status,
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": (
            "provider_reported"
            if usage_status == "reported"
            else (
                "not_applicable"
                if usage_status == "not_applicable"
                else "unavailable"
            )
        ),
        "provider_usage_snapshot": None,
        "unavailable_reason": unavailable_reason,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (
            input_tokens + output_tokens
            if isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            else None
        ),
        "started_at": "2026-07-27T00:00:00Z",
        "finished_at": "2026-07-27T00:00:01Z",
        "wall_time_seconds": 1.0,
        "source_artifact_path": (
            f"model_invocations/{invocation_id}/terminal.json"
        ),
    }


_BUDGET_CLOCK_ONLY = object()


def _authority_record_bytes(record, *, pretty=False):
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _publish_budget_authority(
    state,
    usage,
    *,
    pretty_terminal=False,
    terminal_digest_override=None,
):
    root = Path(state["authority_root"])
    invocation_dir = root / "model_invocations" / usage["invocation_id"]
    invocation_dir.mkdir(parents=True, exist_ok=True)
    start_record = {"invocation_id": usage["invocation_id"]}
    start_bytes = _authority_record_bytes(start_record)
    terminal_bytes = _authority_record_bytes(
        usage,
        pretty=pretty_terminal,
    )
    start_digest = hashlib.sha256(start_bytes).hexdigest()
    terminal_digest = hashlib.sha256(terminal_bytes).hexdigest()
    started_path = invocation_dir / f"started-{start_digest}.json"
    terminal_path = invocation_dir / f"terminal-{terminal_digest}.json"
    if not started_path.exists():
        started_path.write_bytes(start_bytes)
    if not terminal_path.exists():
        terminal_path.write_bytes(terminal_bytes)
    events = [
            {
                "event_id": f"START-{usage['invocation_id']}",
                "event_type": "model_invocation_started",
                "source_event_id": usage["invocation_id"],
                "payload": {
                    **start_record,
                    "_source_artifact_path": (
                        started_path.relative_to(root).as_posix()
                    ),
                    "_source_record_sha256": start_digest,
                },
            },
            {
                "event_id": usage["usage_event_id"],
                "event_type": "model_invocation_usage_recorded",
                "source_event_id": usage["usage_event_id"],
                "payload": {
                    **copy.deepcopy(usage),
                    "_source_artifact_path": (
                        terminal_path.relative_to(root).as_posix()
                    ),
                    "_source_record_sha256": (
                        terminal_digest_override
                        or terminal_digest
                    ),
                },
            },
    ]
    events_path = root / state["authority_events_path"]
    existing_events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with events_path.open("a", encoding="utf-8") as stream:
        for event in events:
            if event in existing_events:
                continue
            stream.write(json.dumps(event, sort_keys=True) + "\n")


def _advance_budget(
    state,
    terminal_usage=_BUDGET_CLOCK_ONLY,
    **kwargs,
):
    if terminal_usage is _BUDGET_CLOCK_ONLY:
        return advance_experiment_budget(state, **kwargs)
    if terminal_usage is None:
        return advance_experiment_budget(state, None, **kwargs)
    _publish_budget_authority(state, terminal_usage)
    return advance_experiment_budget(
        state,
        terminal_usage["usage_event_id"],
        **kwargs,
    )


class ExperimentBudgetTests(unittest.TestCase):
    def _authority_paths(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        events_path = root / "events.jsonl"
        events_path.touch()
        return root, events_path

    def _state(
        self,
        *,
        max_total_tokens=100,
        max_wall_time_seconds=100,
        soft_warning_ratio=0.8,
        initial_monotonic=10,
    ):
        authority_root, authority_events_path = self._authority_paths()
        return create_experiment_budget_state(
            "protocol-global-fixture",
            max_total_tokens,
            max_wall_time_seconds,
            soft_warning_ratio,
            authority_root=authority_root,
            authority_events_path=authority_events_path,
            initial_monotonic=initial_monotonic,
        )

    def _event_validator(self):
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "experiment_budget_event.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_components_stay_separate_and_events_match_declared_schema(self):
        state = self._state(max_total_tokens=200, soft_warning_ratio=0.5)
        usage = _budget_usage(
            "components",
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=30,
            reasoning_tokens=20,
        )

        state, events = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )

        self.assertEqual(
            {
                field: state[field]
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                    "overshoot_tokens",
                )
            },
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 30,
                "reasoning_tokens": 20,
                "total_tokens": 130,
                "overshoot_tokens": 0,
            },
        )
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        validator = self._event_validator()
        validator.validate(events[0])

        invalid = copy.deepcopy(events[0])
        invalid["max_total_tokens"] = True
        self.assertTrue(list(validator.iter_errors(invalid)))
        invalid = copy.deepcopy(events[0])
        invalid["elapsed_wall_time_seconds"] = float("inf")
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_token_warning_and_exhaustion_include_exact_boundaries(self):
        state = self._state()
        state, events = _advance_budget(
            state,
            _budget_usage("below-warning", input_tokens=79, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(events, [])

        state, events = _advance_budget(
            state,
            _budget_usage("at-warning", input_tokens=1, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        self.assertEqual(events[0]["threshold_dimensions"], ["tokens"])

        state, events = _advance_budget(
            state,
            _budget_usage("at-limit", input_tokens=20, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["exhaustion"],
        )
        self.assertTrue(state["exhausted"])
        self.assertEqual(state["overshoot_tokens"], 0)

        state, events = _advance_budget(
            state,
            _budget_usage("above-limit", input_tokens=1, output_tokens=0),
            now_monotonic=10,
        )
        self.assertEqual(events, [])
        self.assertEqual(state["overshoot_tokens"], 1)

    def test_fake_monotonic_clock_drives_exact_wall_boundaries(self):
        ticks = iter([10, 17.999, 18, 30])
        controller = ExperimentBudgetController(monotonic=lambda: next(ticks))
        authority_root, authority_events_path = self._authority_paths()
        state = controller.create_state(
            "clock-fixture",
            100,
            20,
            0.4,
            authority_root=authority_root,
            authority_events_path=authority_events_path,
        )

        state, events = controller.advance(state)
        self.assertEqual(events, [])
        state, events = controller.advance(state)
        self.assertEqual([event["event_kind"] for event in events], ["warning"])
        self.assertEqual(state["elapsed_wall_time_seconds"], 8.0)
        state, events = controller.advance(state)
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["exhaustion"],
        )
        self.assertEqual(events[0]["threshold_dimensions"], ["wall_time"])

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "regressed",
        ):
            _advance_budget(state, now_monotonic=29)

    def test_simultaneous_threshold_events_emit_exactly_once(self):
        state = self._state(
            max_total_tokens=100,
            max_wall_time_seconds=10,
            soft_warning_ratio=0.5,
            initial_monotonic=0,
        )
        usage = _budget_usage("simultaneous", input_tokens=100, output_tokens=0)
        state, events = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["warning", "exhaustion"],
        )
        self.assertTrue(
            all(
                event["threshold_dimensions"] == ["tokens", "wall_time"]
                for event in events
            )
        )
        frozen_projection = json.dumps(state, sort_keys=True)

        replayed, replay_events = _advance_budget(
            state,
            copy.deepcopy(usage),
            now_monotonic=10,
        )
        self.assertEqual(replay_events, [])
        self.assertEqual(json.dumps(replayed, sort_keys=True), frozen_projection)
        self.assertEqual(
            [event["event_kind"] for event in replayed["events"]],
            ["warning", "exhaustion"],
        )

    def test_one_lane_terminal_completion_exposes_overshoot(self):
        state = self._state()
        state, _ = _advance_budget(
            state,
            _budget_usage("before-limit", input_tokens=70, output_tokens=20),
            now_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            _budget_usage(
                "inflight-completion",
                input_tokens=20,
                cached_input_tokens=5,
                output_tokens=5,
                reasoning_tokens=2,
            ),
            now_monotonic=11,
        )

        self.assertEqual(state["total_tokens"], 115)
        self.assertEqual(state["input_tokens"], 90)
        self.assertEqual(state["output_tokens"], 25)
        self.assertEqual(state["cached_input_tokens"], 5)
        self.assertEqual(state["reasoning_tokens"], 2)
        self.assertEqual(state["overshoot_tokens"], 15)
        self.assertEqual(events[-1]["event_kind"], "exhaustion")
        self.assertEqual(events[-1]["overshoot_tokens"], 15)

    def test_unavailable_partial_not_applicable_and_missing_fail_closed(self):
        cases = {
            "partial": _budget_usage(
                "partial",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="partial",
                unavailable_reason="incomplete_provider_usage",
            ),
            "unavailable": _budget_usage(
                "unavailable",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="unavailable",
                unavailable_reason="missing_provider_terminal_usage",
            ),
            "not-applicable": _budget_usage(
                "not-applicable",
                input_tokens=None,
                output_tokens=None,
                cached_input_tokens=None,
                reasoning_tokens=None,
                usage_status="not_applicable",
            ),
            "missing-total": _budget_usage(
                "missing-total",
                input_tokens=1,
                output_tokens=1,
            ),
            "missing-terminal": None,
        }
        cases["missing-total"]["total_tokens"] = None

        for name, usage in cases.items():
            with self.subTest(name=name):
                state, _ = _advance_budget(
                    self._state(),
                    usage,
                    now_monotonic=10,
                )
                self.assertEqual(state["total_tokens"], 0)
                self.assertFalse(state["usage_complete"])
                self.assertFalse(state["calibration_eligible"])
                self.assertTrue(state["incomplete_usage_reasons"])

        state = self._state(
            max_wall_time_seconds=1,
            initial_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            cases["unavailable"],
            now_monotonic=11,
        )
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["warning", "exhaustion"],
        )
        validator = self._event_validator()
        for event in events:
            validator.validate(event)
            self.assertFalse(event["usage_complete"])
            self.assertEqual(
                event["incomplete_usage_reasons"][0]["usage_status"],
                "unavailable",
            )

    def test_replay_is_idempotent_and_conflicting_identity_fails_closed(self):
        state = self._state()
        usage = _budget_usage("replay", input_tokens=70, output_tokens=10)
        state, _ = _advance_budget(
            state,
            usage,
            now_monotonic=10,
        )
        replayed, events = _advance_budget(
            state,
            copy.deepcopy(usage),
            now_monotonic=10,
        )
        self.assertEqual(replayed, state)
        self.assertEqual(events, [])

        rebuilt_state = create_experiment_budget_state(
            state["budget_id"],
            state["max_total_tokens"],
            state["max_wall_time_seconds"],
            state["soft_warning_ratio"],
            authority_root=state["authority_root"],
            authority_events_path=state["authority_events_path"],
            initial_monotonic=state["initial_monotonic"],
        )
        rebuilt, rebuilt_events = advance_experiment_budget(
            rebuilt_state,
            usage["usage_event_id"],
            now_monotonic=10,
        )
        self.assertEqual(rebuilt, state)
        self.assertEqual(
            [event["event_id"] for event in rebuilt_events],
            [event["event_id"] for event in state["events"]],
        )

        conflict = copy.deepcopy(usage)
        conflict["input_tokens"] = 71
        conflict["total_tokens"] = 81
        conflict_state = self._state()
        conflict_state, _ = _advance_budget(
            conflict_state,
            usage,
            now_monotonic=10,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            _advance_budget(
                conflict_state,
                conflict,
                now_monotonic=10,
            )

        duplicate_invocation = copy.deepcopy(usage)
        duplicate_invocation["usage_event_id"] = usage_event_id_for_invocation(
            "INV-replay-second"
        )
        duplicate_state = self._state()
        duplicate_state, _ = _advance_budget(
            duplicate_state,
            usage,
            now_monotonic=10,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            _advance_budget(
                duplicate_state,
                duplicate_invocation,
                now_monotonic=10,
            )

    def test_serialized_projection_cannot_reset_consumption_before_replay(self):
        usage = _budget_usage(
            "projection-reset",
            input_tokens=10,
            output_tokens=2,
        )
        state, _ = _advance_budget(
            self._state(),
            usage,
            now_monotonic=10,
        )
        tampered = copy.deepcopy(state)
        tampered["input_tokens"] = 0
        tampered["output_tokens"] = 0
        tampered["total_tokens"] = 0

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "projection integrity",
        ):
            validate_experiment_budget_state(tampered)
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "projection integrity",
        ):
            _advance_budget(
                tampered,
                copy.deepcopy(usage),
                now_monotonic=10,
            )

    def test_only_canonical_phase1_terminal_usage_can_add_consumption(self):
        canonical = _budget_usage(
            "canonical-authority",
            input_tokens=10,
            output_tokens=2,
        )
        rejected, _ = advance_experiment_budget(
            self._state(),
            canonical,
            now_monotonic=10,
        )
        self.assertEqual(rejected["total_tokens"], 0)
        self.assertFalse(rejected["calibration_eligible"])
        self.assertEqual(
            rejected["incomplete_usage_reasons"][0]["reason"],
            "missing_terminal_usage_authority",
        )

        state, _ = _advance_budget(
            self._state(),
            canonical,
            now_monotonic=10,
        )
        self.assertEqual(state["total_tokens"], 12)
        digest_mismatch_state = self._state()
        _publish_budget_authority(
            digest_mismatch_state,
            canonical,
            terminal_digest_override="b" * 64,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "source-record digest mismatch",
        ):
            advance_experiment_budget(
                digest_mismatch_state,
                canonical["usage_event_id"],
                now_monotonic=10,
            )

        _publish_budget_authority(
            state,
            canonical,
            pretty_terminal=True,
        )
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "authority replay failed",
        ):
            advance_experiment_budget(
                state,
                canonical["usage_event_id"],
                now_monotonic=10,
            )

        for name, mutate in (
            (
                "noncanonical-event-id",
                lambda usage: usage.update(
                    {"usage_event_id": "USAGE-has space"}
                ),
            ),
            (
                "incomplete-terminal-shape",
                lambda usage: usage.pop("source_artifact_path"),
            ),
        ):
            with self.subTest(name=name):
                usage = _budget_usage(
                    name,
                    input_tokens=10,
                    output_tokens=2,
                )
                mutate(usage)
                rejected, events = _advance_budget(
                    self._state(
                        max_wall_time_seconds=1,
                        initial_monotonic=10,
                    ),
                    usage,
                    now_monotonic=11,
                )
                self.assertEqual(rejected["total_tokens"], 0)
                self.assertFalse(rejected["calibration_eligible"])
                self.assertIsNone(
                    rejected["incomplete_usage_reasons"][0][
                        "usage_event_id"
                    ]
                    if name == "noncanonical-event-id"
                    else None
                )
                for event in events:
                    self._event_validator().validate(event)

    def test_budget_state_v2_rejects_unsealed_v1_state(self):
        state = self._state()
        legacy = copy.deepcopy(state)
        legacy["budget_schema_version"] = "experiment_budget_state.v1"
        legacy.pop("projection_sha256")

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "fields do not match|unsupported",
        ):
            validate_experiment_budget_state(legacy)

    def test_authority_root_symlink_and_event_log_replacement_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            real_root = parent / "real-authority"
            real_root.mkdir()
            events_path = real_root / "events.jsonl"
            events_path.touch()
            linked_root = parent / "linked-authority"
            linked_root.symlink_to(real_root, target_is_directory=True)

            with self.assertRaisesRegex(
                ExperimentBudgetIntegrityError,
                "symbolic link",
            ):
                create_experiment_budget_state(
                    "symlink-authority",
                    100,
                    100,
                    0.8,
                    authority_root=linked_root,
                    authority_events_path=linked_root / "events.jsonl",
                    initial_monotonic=0,
                )

            fifo_root = parent / "fifo-authority"
            fifo_root.mkdir()
            fifo_events = fifo_root / "events.jsonl"
            os.mkfifo(fifo_events)
            with self.assertRaisesRegex(
                ExperimentBudgetIntegrityError,
                "regular authority file",
            ):
                create_experiment_budget_state(
                    "fifo-authority",
                    100,
                    100,
                    0.8,
                    authority_root=fifo_root,
                    authority_events_path=fifo_events,
                    initial_monotonic=0,
                )

        state = self._state()
        usage = _budget_usage(
            "event-log-replacement",
            input_tokens=1,
            output_tokens=1,
        )
        events_path = (
            Path(state["authority_root"]) / state["authority_events_path"]
        )
        replacement = events_path.with_name("replacement-events.jsonl")
        replacement.touch()
        os.replace(replacement, events_path)
        _publish_budget_authority(state, usage)
        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "identity changed",
        ):
            advance_experiment_budget(
                state,
                usage["usage_event_id"],
                now_monotonic=10,
            )

    def test_authority_event_log_history_cannot_be_truncated_in_place(self):
        state = self._state()
        first = _budget_usage(
            "append-only-first",
            input_tokens=10,
            output_tokens=1,
        )
        state, _ = _advance_budget(
            state,
            first,
            now_monotonic=10,
        )
        events_path = (
            Path(state["authority_root"]) / state["authority_events_path"]
        )
        original_inode = events_path.stat().st_ino
        events_path.write_text("", encoding="utf-8")
        self.assertEqual(events_path.stat().st_ino, original_inode)
        second = _budget_usage(
            "append-only-second",
            input_tokens=20,
            output_tokens=2,
        )
        _publish_budget_authority(state, second)

        with self.assertRaisesRegex(
            ExperimentBudgetIntegrityError,
            "append-only prefix changed",
        ):
            advance_experiment_budget(
                state,
                second["usage_event_id"],
                now_monotonic=11,
            )

    def test_frozen_budget_drift_is_rejected_and_exhaustion_is_sticky(self):
        state = self._state()
        state, _ = _advance_budget(
            state,
            _budget_usage("exhaust", input_tokens=100, output_tokens=0),
            now_monotonic=10,
        )
        state, events = _advance_budget(
            state,
            _budget_usage("post-exhaust", input_tokens=1, output_tokens=0),
            now_monotonic=11,
        )
        self.assertTrue(state["exhausted"])
        self.assertEqual(events, [])

        for field, value in (
            ("budget_id", "different-budget"),
            ("max_total_tokens", 1000),
            ("max_wall_time_seconds", 1000),
            ("soft_warning_ratio", 0.5),
            ("initial_monotonic", 0),
        ):
            with self.subTest(field=field):
                drifted = copy.deepcopy(state)
                drifted[field] = value
                with self.assertRaisesRegex(
                    ExperimentBudgetIntegrityError,
                    "frozen experiment budget",
                ):
                    validate_experiment_budget_state(drifted)

    def test_invalid_numeric_inputs_never_become_consumption(self):
        authority_root, authority_events_path = self._authority_paths()
        for name, kwargs in {
            "boolean-token-limit": {"max_total_tokens": True},
            "zero-token-limit": {"max_total_tokens": 0},
            "nonfinite-wall-limit": {
                "max_wall_time_seconds": float("inf")
            },
            "zero-ratio": {"soft_warning_ratio": 0},
            "unit-ratio": {"soft_warning_ratio": 1},
        }.items():
            with self.subTest(name=name):
                arguments = {
                    "budget_id": "invalid-fixture",
                    "max_total_tokens": 100,
                    "max_wall_time_seconds": 100,
                    "soft_warning_ratio": 0.8,
                    "initial_monotonic": 0,
                    "authority_root": authority_root,
                    "authority_events_path": authority_events_path,
                }
                arguments.update(kwargs)
                with self.assertRaises(ExperimentBudgetError):
                    create_experiment_budget_state(**arguments)

        invalid_usages = []
        for field, value in (
            ("input_tokens", -1),
            ("input_tokens", True),
            ("cached_input_tokens", -1),
            ("reasoning_tokens", True),
        ):
            usage = _budget_usage(
                f"invalid-{field}-{value}",
                input_tokens=1,
                output_tokens=1,
            )
            usage[field] = value
            invalid_usages.append(usage)
        inconsistent = _budget_usage(
            "inconsistent-total",
            input_tokens=1,
            output_tokens=1,
        )
        inconsistent["total_tokens"] = 3
        invalid_usages.append(inconsistent)

        for usage in invalid_usages:
            with self.subTest(usage_event_id=usage["usage_event_id"]):
                state, _ = _advance_budget(
                    self._state(),
                    usage,
                    now_monotonic=10,
                )
                self.assertEqual(state["total_tokens"], 0)
                self.assertFalse(state["calibration_eligible"])

        with self.assertRaises(ExperimentBudgetError):
            _advance_budget(
                self._state(),
                now_monotonic=float("nan"),
            )


class ExperimentProviderBudgetBoundaryTests(unittest.TestCase):
    @staticmethod
    def _runner_factory(stdout, calls):
        class FakeGatedRunner:
            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
                environment=None,
            ):
                del lifecycle, cwd, input_text, timeout_seconds, environment
                self.command = list(command)
                calls.append("constructed")

            def prepare(self):
                calls.append("prepared")
                return ExecutionGroupIdentity.not_applicable()

            def permit_and_wait(self, **_kwargs):
                calls.append("permitted")
                return ProviderExecution(self.command, 0, stdout, "")

            def abort_before_permit(self):
                calls.append("aborted")

            def cleanup_after_terminal(self):
                calls.append("cleaned")

        return FakeGatedRunner

    @staticmethod
    def _usage_stdout(input_tokens, output_tokens):
        return json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                    "total_tokens": input_tokens + output_tokens,
                },
            },
            sort_keys=True,
        )

    def _controller(self, root, *, max_total_tokens=100):
        return create_experiment_controller(
            root,
            protocol_id="phase2-provider-boundary",
            max_total_tokens=max_total_tokens,
            max_wall_time_seconds=3600,
            soft_warning_ratio=0.8,
            scored=True,
        )

    def _call(
        self,
        root,
        controller,
        run_name,
        stdout,
        calls,
        *,
        supported=True,
    ):
        lifecycle_root = root / "runs" / run_name
        lifecycle_root.mkdir(parents=True)
        context = _model_context(
            supported=supported,
            sandbox_reference=None,
        )
        context.update(
            {
                "run_id": f"RUN-{run_name}",
                "task_id": "P2-03B",
                "attempt_id": f"ATTEMPT-{run_name}",
                "runtime_execution_session_id": f"SESSION-{run_name}",
                "lifecycle_owner_token": f"OWNER-{run_name}",
                "experiment_authority_root": str(root),
                "experiment_controller_reference": controller.reference,
            }
        )
        return ModelInvocationCall(
            lifecycle_root,
            context,
            supported=supported,
            systemd_runner_factory=self._runner_factory(stdout, calls),
        )

    @staticmethod
    def _execute(call, root):
        return call.execute(
            [str(Path(sys.executable).resolve()), "-c", "pass"],
            cwd=root,
            input_text="prompt",
            timeout_seconds=10,
        )

    def test_protocol_global_cross_run_lease_serializes_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_calls = []
            first = self._call(
                root,
                controller,
                "FIRST",
                self._usage_stdout(7, 3),
                first_calls,
            )
            first_execution = self._execute(first, root)

            second_calls = []
            second = self._call(
                root,
                controller,
                "SECOND",
                self._usage_stdout(5, 1),
                second_calls,
            )
            with self.assertRaisesRegex(
                ModelInvocationUnavailable,
                "protocol-global provider lane",
            ):
                self._execute(second, root)
            self.assertEqual(second_calls, [])
            self.assertFalse(second.lifecycle.started_path.exists())

            first.finalize("completed", first_execution)
            second_execution = self._execute(second, root)
            second.finalize("completed", second_execution)
            self.assertEqual(controller.budget_state["total_tokens"], 16)

    def test_provider_lane_inode_replacement_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_root = root / "runs" / "FIRST"
            second_root = root / "runs" / "SECOND"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            first = controller.prelaunch_admission(
                {
                    "invocation_id": "INV-FIRST",
                    "lifecycle_root": str(first_root),
                    "run_id": "RUN-FIRST",
                }
            )
            lane_path = root / "experiment-provider-lane.lock"
            lane_path.unlink()
            lane_path.touch()

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "provider lane authority identity changed",
            ):
                controller.prelaunch_admission(
                    {
                        "invocation_id": "INV-SECOND",
                        "lifecycle_root": str(second_root),
                        "run_id": "RUN-SECOND",
                    }
                )
            self.assertTrue(first.held)
            first._release()

    def test_controller_lock_and_event_inode_replacement_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            state_lock = root / "experiment-budget-controller-state.lock"
            replacement = root / "replacement-state-lock"
            replacement.touch()
            os.replace(replacement, state_lock)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "controller lock authority identity changed",
            ):
                controller.snapshot()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            state_journal = (
                root / "experiment-budget-controller-state.jsonl"
            )
            replacement = root / "replacement-state-journal"
            replacement.touch()
            os.replace(replacement, state_journal)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "state_journal authority identity changed",
            ):
                type(controller)(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            budget_events = root / "experiment-budget-events.jsonl"
            replacement = root / "replacement-budget-events"
            replacement.touch()
            os.replace(replacement, budget_events)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "budget_events authority identity changed",
            ):
                type(controller)(root)

    def test_controller_rejects_fifo_authority_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            os.mkfifo(root / "experiment-provider-lane.lock")
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "authority path is not a regular file",
            ):
                self._controller(root)

    def test_denied_prelaunch_creates_no_runner_start_or_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            admitted_calls = []
            admitted = self._call(
                root,
                controller,
                "EXHAUST",
                self._usage_stdout(8, 2),
                admitted_calls,
            )
            execution = self._execute(admitted, root)
            admitted.finalize("completed", execution)

            denied_calls = []
            denied = self._call(
                root,
                controller,
                "DENIED",
                self._usage_stdout(1, 0),
                denied_calls,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ModelInvocationUnavailable,
                    "budget exhausted",
                ):
                    self._execute(denied, root)
            self.assertEqual(denied_calls, [])
            popen.assert_not_called()
            self.assertFalse(denied.lifecycle.started_path.exists())

    def test_required_controller_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lifecycle_root = root / "runs" / "MISSING"
            lifecycle_root.mkdir(parents=True)
            calls = []
            context = _model_context(
                supported=True,
                sandbox_reference=None,
            )
            context.update(
                {
                    "run_id": "RUN-MISSING",
                    "task_id": "P2-03B",
                    "attempt_id": "ATTEMPT-MISSING",
                    "runtime_execution_session_id": "SESSION-MISSING",
                    "lifecycle_owner_token": "OWNER-MISSING",
                    "experiment_authority_root": str(root),
                    "experiment_controller_required": True,
                }
            )
            invocation = ModelInvocationCall(
                lifecycle_root,
                context,
                supported=True,
                systemd_runner_factory=self._runner_factory("", calls),
            )

            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "required experiment budget controller is unavailable",
            ):
                self._execute(invocation, root)
            self.assertEqual(calls, [])
            self.assertFalse(invocation.lifecycle.started_path.exists())

    def test_controller_reference_rejects_alternate_protocol_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_controller = self._controller(first_root)
            self._controller(second_root)
            lifecycle_root = second_root / "runs" / "MISMATCH"
            lifecycle_root.mkdir(parents=True)
            calls = []
            context = _model_context(
                supported=True,
                sandbox_reference=None,
            )
            context.update(
                {
                    "run_id": "RUN-MISMATCH",
                    "task_id": "P2-03B",
                    "attempt_id": "ATTEMPT-MISMATCH",
                    "runtime_execution_session_id": "SESSION-MISMATCH",
                    "lifecycle_owner_token": "OWNER-MISMATCH",
                    "experiment_authority_root": str(second_root),
                    "experiment_controller_reference": (
                        first_controller.reference
                    ),
                    "experiment_controller_required": True,
                }
            )
            invocation = ModelInvocationCall(
                lifecycle_root,
                context,
                supported=True,
                systemd_runner_factory=self._runner_factory("", calls),
            )

            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "reference authority changed",
            ):
                self._execute(invocation, second_root)
            self.assertEqual(calls, [])

    def test_accounting_occurs_only_after_authoritative_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "TERMINAL",
                self._usage_stdout(9, 2),
                calls,
            )
            execution = self._execute(invocation, root)

            self.assertFalse(invocation.lifecycle.terminal_path.exists())
            self.assertEqual(controller.budget_state["total_tokens"], 0)
            terminal = invocation.finalize("completed", execution)

            self.assertTrue(invocation.lifecycle.terminal_path.is_file())
            state = controller.budget_state
            self.assertEqual(state["total_tokens"], 11)
            self.assertEqual(
                state["invocation_usage_events"],
                {terminal["invocation_id"]: terminal["usage_event_id"]},
            )
            event_types = [
                json.loads(line)["event_type"]
                for line in (root / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                event_types,
                [
                    "model_invocation_started",
                    "model_invocation_usage_recorded",
                ],
            )

    def test_terminal_before_projection_is_recovered_before_lane_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            first_calls = []
            first = self._call(
                root,
                controller,
                "RECOVERY-FIRST",
                self._usage_stdout(4, 2),
                first_calls,
            )
            first_execution = self._execute(first, root)
            first.lifecycle.finalize(
                "completed",
                stdout=first_execution.stdout,
                stderr=first_execution.stderr,
            )
            first.provider_admission._release()

            second_calls = []
            second = self._call(
                root,
                controller,
                "RECOVERY-SECOND",
                self._usage_stdout(3, 1),
                second_calls,
            )
            second_execution = self._execute(second, root)
            self.assertEqual(controller.budget_state["total_tokens"], 6)
            second.finalize("completed", second_execution)
            self.assertEqual(controller.budget_state["total_tokens"], 10)

    def test_started_orphan_is_terminalized_before_lane_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "ORPHAN",
                self._usage_stdout(4, 2),
                calls,
            )
            self._execute(invocation, root)
            invocation.provider_admission._release()

            reconciliation = reconcile_orphaned_invocation(
                invocation.lifecycle.authority_root,
                {
                    "attempt_id": "ATTEMPT-ORPHAN",
                    "lease_id": "OWNER-ORPHAN",
                    "agent_id": "agent-fixture",
                },
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "test_process_death",
                },
            )
            self.assertEqual(
                reconciliation["reconciliation_status"],
                "recovered",
            )
            self.assertTrue(invocation.lifecycle.terminal_path.is_file())

            next_root = root / "runs" / "AFTER-ORPHAN"
            next_root.mkdir(parents=True)
            with self.assertRaisesRegex(
                ExperimentProviderAdmissionDenied,
                "controller state is budget_draining",
            ):
                controller.prelaunch_admission(
                    {
                        "invocation_id": "INV-AFTER-ORPHAN",
                        "lifecycle_root": str(next_root),
                        "run_id": "RUN-AFTER-ORPHAN",
                    }
                )
            self.assertEqual(
                controller.controller_status,
                "budget_draining",
            )
            self.assertFalse(controller.budget_state["usage_complete"])

    def test_exact_exhaustion_is_sticky_and_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            calls = []
            invocation = self._call(
                root,
                controller,
                "EXACT",
                self._usage_stdout(6, 4),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)

            state = controller.budget_state
            self.assertEqual(state["total_tokens"], 10)
            self.assertEqual(state["overshoot_tokens"], 0)
            self.assertTrue(state["exhausted"])
            self.assertEqual(
                [event["event_kind"] for event in state["events"]],
                ["warning", "exhaustion"],
            )
            self.assertEqual(
                controller.controller_status,
                "budget_draining",
            )
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "cannot be reset or extended",
            ):
                self._controller(root, max_total_tokens=11)
            observed = controller.observe_boundary(
                "post_integration",
                scheduler_inflight=0,
                integration_active=False,
            )
            self.assertEqual(
                observed["controller_status"],
                "budget_stopped",
            )
            with self.assertRaisesRegex(
                ExperimentControllerError,
                "only interrupted",
            ):
                controller.resume_interrupted()

    def test_valid_state_snapshot_rollback_restores_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root, max_total_tokens=10)
            state_path = root / "experiment-budget-controller-state.json"
            pre_usage_snapshot = state_path.read_bytes()
            calls = []
            invocation = self._call(
                root,
                controller,
                "ROLLBACK",
                self._usage_stdout(8, 2),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)
            self.assertTrue(controller.budget_state["exhausted"])

            state_path.write_bytes(pre_usage_snapshot)
            recovered = type(controller)(root)
            self.assertEqual(recovered.budget_state["total_tokens"], 10)
            self.assertTrue(recovered.budget_state["exhausted"])
            self.assertEqual(
                recovered.controller_status,
                "budget_draining",
            )

            denied = self._call(
                root,
                recovered,
                "ROLLBACK-DENIED",
                self._usage_stdout(1, 0),
                [],
            )
            with self.assertRaisesRegex(
                ModelInvocationUnavailable,
                "budget exhausted",
            ):
                self._execute(denied, root)

    def test_state_journal_truncation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            controller.interrupt()
            journal_path = (
                root / "experiment-budget-controller-state.jsonl"
            )
            first_checkpoint = journal_path.read_bytes().splitlines()[0]
            journal_path.write_bytes(first_checkpoint + b"\n")

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "state is ahead of its append-first journal",
            ):
                type(controller)(root)

    def test_torn_state_journal_tail_recovers_last_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            controller.interrupt()
            journal_path = (
                root / "experiment-budget-controller-state.jsonl"
            )
            committed = journal_path.read_bytes()
            journal_path.write_bytes(committed + b'{"schema_version":')

            recovered = type(controller)(root)

            self.assertEqual(recovered.controller_status, "interrupted")
            self.assertEqual(journal_path.read_bytes(), committed)

    def test_interruption_resume_preserves_original_remaining_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = self._controller(root)
            calls = []
            invocation = self._call(
                root,
                controller,
                "INTERRUPTED",
                self._usage_stdout(12, 3),
                calls,
            )
            execution = self._execute(invocation, root)
            invocation.finalize("completed", execution)
            controller.interrupt()

            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "extension or reduction",
            ):
                controller.resume_interrupted(max_total_tokens=101)
            resumed = controller.resume_interrupted(
                reference=controller.reference,
                max_total_tokens=100,
                max_wall_time_seconds=3600,
            )
            self.assertEqual(resumed["controller_status"], "active")
            self.assertEqual(
                resumed["budget_state"]["total_tokens"],
                15,
            )


class _SchedulerMonotonic:
    def __init__(self, value=None):
        self.value = time.monotonic() if value is None else value

    def __call__(self):
        self.value = max(self.value, time.monotonic())
        return self.value

    def advance(self, seconds):
        self.value += seconds


class _SchedulerClock:
    def now(self):
        return "2026-07-27T19:00:00Z"


class TwoPhaseSchedulerExperimentBoundaryTests(unittest.TestCase):
    def _controller(
        self,
        output_dir,
        monotonic,
        *,
        max_total_tokens=100,
        max_wall_time_seconds=60,
    ):
        return create_experiment_controller(
            output_dir,
            protocol_id="phase2-scheduler-boundary",
            max_total_tokens=max_total_tokens,
            max_wall_time_seconds=max_wall_time_seconds,
            soft_warning_ratio=0.8,
            scored=True,
            initial_monotonic=monotonic.value,
            monotonic=monotonic,
        )

    def _scheduler(
        self,
        root,
        controller,
        monotonic,
        *,
        write_scope=None,
        project_root=None,
        verification_command=None,
        commit_verified_integration=False,
        resume_interrupted_experiment=False,
        invocation_fence_assessor=None,
        task_overrides=None,
        max_inflight=1,
        task_count=1,
    ):
        output_dir = controller.root
        task = {
            "task_id": "P2-03B-SCHEDULER",
            "milestone_id": "M0",
            "objective": "Exercise scheduler experiment boundaries.",
            "backlog_status": "ready",
            "risk_target": "L0",
            "depends_on": [],
            "read_scope": ["."],
            "write_scope": list(write_scope or []),
            "required_role": "implementation_worker",
            "blockers": [],
        }
        task.update(task_overrides or {})
        backlog_path = root / "backlog.json"
        tasks = [task]
        for index in range(2, task_count + 1):
            additional = copy.deepcopy(task)
            additional["task_id"] = f"P2-03B-SCHEDULER-{index}"
            tasks.append(additional)
        backlog_path.write_text(
            json.dumps(
                {"backlog_id": "BL-P2-03B", "items": tasks},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        agent_pool_path = root / "agent_pool.json"
        agent_pool_path.write_text(
            json.dumps(
                {
                    "pool_id": "phase2-scheduler-pool",
                    "scheduler_agent_id": "agent-scheduler",
                    "updated_at": "2026-07-27T19:00:00Z",
                    "agents": [
                        {
                            "agent_id": (
                                "agent-implementation"
                                if index == 1
                                else f"agent-implementation-{index}"
                            ),
                            "role": "implementation_worker",
                            "status": "idle",
                            "model_profile": "test",
                            "runtime_adapter": "codex",
                            "subscriptions": [],
                            "inbox_path": (
                                "mailboxes/agent-implementation/inbox.jsonl"
                                if index == 1
                                else f"mailboxes/agent-implementation-{index}/inbox.jsonl"
                            ),
                            "outbox_path": (
                                "mailboxes/agent-implementation/outbox.jsonl"
                                if index == 1
                                else f"mailboxes/agent-implementation-{index}/outbox.jsonl"
                            ),
                            "lease": {
                                "lease_id": None,
                                "task_id": None,
                                "expires_at": None,
                            },
                            "owned_artifacts": [],
                            "last_event_id": None,
                            "memory_summary_path": None,
                        }
                        for index in range(1, task_count + 1)
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=_SchedulerClock(),
            project_root=project_root,
            max_inflight=max_inflight,
            integrate_accepted_patch=project_root is not None,
            integration_verification_command=verification_command,
            commit_verified_integration=commit_verified_integration,
            experiment_controller_reference=controller.reference,
            experiment_controller_required=True,
            resume_interrupted_experiment=resume_interrupted_experiment,
            experiment_controller_monotonic=monotonic,
            invocation_fence_assessor=invocation_fence_assessor,
        )

    @classmethod
    def _append_result(
        cls,
        inflight,
        changed_files,
        *,
        output=None,
        publish_terminal=True,
    ):
        if publish_terminal:
            cls._publish_zero_usage_terminal(inflight)
        record = {
            "message_id": f"RESULT-{inflight['message_id']}",
            "from_agent": inflight["agent_id"],
            "to_agent": "agent-scheduler",
            "message_type": "runtime_result",
            "correlation_id": inflight["correlation_id"],
            "created_at": "2026-07-27T19:00:01Z",
            "payload": {
                "source_message_id": inflight["message_id"],
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "lease_id": inflight["lease_id"],
                "result_status": "completed",
                "changed_files": list(changed_files),
                "output": output or {"test": "phase2-scheduler-boundary"},
            },
        }
        outbox_path = Path(inflight["outbox_path"])
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with outbox_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")

    @staticmethod
    def _publish_zero_usage_terminal(inflight):
        output_dir = Path(inflight["step_dir"]).parents[1]
        matching_terminals = [
            terminal_path
            for terminal_path in output_dir.glob(
                "model_invocations/*/terminal.json"
            )
            if json.loads(
                terminal_path.read_text(encoding="utf-8")
            ).get("attempt_id")
            == inflight["attempt_id"]
        ]
        if matching_terminals:
            return
        inbox_paths = list(
            Path(inflight["step_dir"]).glob(
                "mailboxes/*/inbox.jsonl"
            )
        )
        if len(inbox_paths) != 1:
            raise AssertionError("expected one scheduler inbox fixture")
        message = json.loads(
            inbox_paths[0].read_text(encoding="utf-8").splitlines()[0]
        )
        context = _model_context(
            supported=True,
            sandbox_reference=None,
        )
        context.update(invocation_context_from_message(message))
        context["coverage_class"] = "supported_model_invocation"
        context["backend"] = "codex"
        calls = []
        invocation = ModelInvocationCall(
            output_dir,
            context,
            supported=True,
            systemd_runner_factory=(
                ExperimentProviderBudgetBoundaryTests._runner_factory(
                    ExperimentProviderBudgetBoundaryTests._usage_stdout(
                        0,
                        0,
                    ),
                    calls,
                )
            ),
        )
        execution = ExperimentProviderBudgetBoundaryTests._execute(
            invocation,
            output_dir,
        )
        invocation.finalize("completed", execution)

    @staticmethod
    def _init_repo(repo):
        repo.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "config", "user.email", "agentteam@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "AgentTeam Test"],
            cwd=repo,
            check=True,
        )
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def _head(repo, ref="HEAD"):
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _invocation_for_inflight(
        output_dir,
        controller,
        inflight,
        stdout,
        calls,
    ):
        context = _model_context(supported=True, sandbox_reference=None)
        context.update(
            {
                "run_id": "RUN-TWO-PHASE-SCHEDULER",
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "runtime_execution_session_id": inflight[
                    "runtime_session_id"
                ],
                "lifecycle_owner_token": inflight["lease_id"],
                "agent_id": inflight["agent_id"],
                "experiment_authority_root": str(controller.root),
                "experiment_controller_reference": controller.reference,
                "experiment_controller_required": True,
            }
        )
        return ModelInvocationCall(
            output_dir,
            context,
            supported=True,
            systemd_runner_factory=(
                ExperimentProviderBudgetBoundaryTests._runner_factory(
                    stdout,
                    calls,
                )
            ),
        )

    def test_scheduler_denies_worker_dispatch_after_budget_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(root, controller, monotonic)
            monotonic.advance(2)

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_count"], 0)
            self.assertEqual(dispatch["dispatch_status"], "budget_stopped")
            self.assertEqual(scheduler.state["inflight_attempts"], [])
            self.assertFalse((output_dir / "steps").exists())
            self.assertEqual(controller.controller_status, "budget_stopped")
            self.assertTrue(controller.budget_state["exhausted"])

    def test_scheduler_injects_only_trusted_controller_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                task_overrides={
                    "experiment_authority_root": str(root / "untrusted"),
                    "experiment_controller_reference": {
                        "schema_version": "untrusted-reference"
                    },
                    "experiment_controller_required": False,
                },
            )

            dispatch = scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            inbox = (
                Path(inflight["step_dir"])
                / "mailboxes"
                / "agent-implementation"
                / "inbox.jsonl"
            )
            message = json.loads(inbox.read_text(encoding="utf-8").splitlines()[0])
            payload = message["payload"]

            self.assertEqual(dispatch["dispatch_count"], 1)
            self.assertEqual(
                payload["experiment_controller_reference"],
                controller.reference,
            )
            self.assertTrue(payload["experiment_controller_required"])
            self.assertEqual(
                payload["experiment_authority_root"],
                str(controller.root),
            )
            persisted = json.loads(scheduler.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["experiment_controller_reference"],
                controller.reference,
            )

    def test_experiment_scheduler_serializes_worker_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                max_inflight=2,
                task_count=2,
            )

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_count"], 1)
            self.assertEqual(len(scheduler.state["inflight_attempts"]), 1)
            self.assertEqual(
                scheduler.state["backlog"]["items"][1][
                    "backlog_status"
                ],
                "ready",
            )

    def test_outbox_result_waits_for_provider_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                invocation_fence_assessor=lambda _start: {
                    "fence_status": "live_pinned",
                    "proof": "test_provider_still_live",
                },
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(1, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            self._append_result(
                inflight,
                [],
                publish_terminal=False,
            )
            waiting = scheduler.collect_ready_results()

            self.assertEqual(waiting["collected_count"], 0)
            self.assertEqual(waiting["inflight_count"], 1)
            invocation.finalize("completed", execution)

            collected = scheduler.collect_ready_results()

            self.assertEqual(collected["collected_count"], 1)
            self.assertEqual(collected["inflight_count"], 0)
            self.assertEqual(controller.budget_state["total_tokens"], 2)

    def test_outbox_without_provider_start_remains_inflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            self._append_result(
                inflight,
                [],
                publish_terminal=False,
            )
            outbox_path = Path(inflight["outbox_path"])
            malformed = json.loads(
                outbox_path.read_text(encoding="utf-8")
            )
            malformed["payload"]["changed_files"] = None
            malformed["payload"]["output"] = None
            outbox_path.write_text(
                json.dumps(malformed, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            waiting = scheduler.collect_ready_results()

            self.assertEqual(waiting["collected_count"], 0)
            self.assertEqual(waiting["inflight_count"], 1)
            self.assertEqual(
                inflight["invocation_reconciliation"][
                    "reconciliation_status"
                ],
                "no_invocation",
            )
            inflight["lease_expires_at"] = "2026-07-27T18:59:59Z"

            expired = scheduler.collect_ready_results()

            self.assertEqual(expired["collected_count"], 1)
            self.assertEqual(expired["inflight_count"], 0)
            result = expired["results"][0]
            self.assertEqual(
                result["runtime_output"]["error"],
                "lease_expired_without_provider_start",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "result_status"
                ],
                "completed",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "changed_files_type"
                ],
                "NoneType",
            )
            self.assertEqual(
                result["runtime_output"]["suspicious_outbox_result"][
                    "output_type"
                ],
                "NoneType",
            )

    def test_direct_collect_rejects_unfinished_integration_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            self._append_result(inflight, [])
            scheduler.state["integration_active"] = True
            scheduler.state["integration_attempt_id"] = inflight[
                "attempt_id"
            ]
            scheduler._write_state()

            blocked = scheduler.collect_ready_results()

            self.assertEqual(
                blocked["collect_status"],
                "integration_recovery_required",
            )
            self.assertEqual(blocked["collected_count"], 0)
            self.assertEqual(blocked["inflight_count"], 1)
            self.assertEqual(scheduler.state["steps"], [])

    def test_scheduler_drains_inflight_and_exposes_terminal_overshoot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_total_tokens=10,
            )
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(8, 5),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            invocation.finalize("completed", execution)
            self._append_result(inflight, [])

            collected = scheduler.collect_ready_results()

            self.assertEqual(collected["collected_count"], 1)
            self.assertEqual(collected["inflight_count"], 0)
            self.assertEqual(
                collected["experiment_controller_status"],
                "budget_stopped",
            )
            budget = collected["experiment_budget_state"]
            self.assertEqual(budget["total_tokens"], 13)
            self.assertEqual(budget["overshoot_tokens"], 3)
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(calls, ["constructed", "prepared", "permitted", "cleaned"])

    def test_preintegration_exhaustion_preserves_patch_and_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
                commit_verified_integration=True,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "accepted patch\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            monotonic.advance(2)

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(inflight["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_status"], "preserved")
            self.assertEqual(result["integration_queue_status"], "pending")
            self.assertEqual(
                result["completion_policy"],
                "accepted_patch_preserved_budget_stop",
            )
            self.assertTrue(Path(result["patch_path"]).is_file())
            self.assertIn(
                "accepted patch",
                Path(result["patch_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(self._head(baseline), source_head)
            self.assertFalse((baseline / "feature.txt").exists())
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")
            stopped_tick = scheduler.tick()
            self.assertEqual(stopped_tick["tick_status"], "budget_stopped")
            self.assertEqual(self._head(baseline), source_head)

    def test_budget_stop_is_deferred_through_successful_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('feature.txt').is_file()",
                ],
                commit_verified_integration=True,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "durable integration\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            observations = []
            initial_monotonic = monotonic.value
            original_observe = scheduler.experiment_controller.observe_boundary
            original_verify = two_phase_scheduler_module.run_integration_verification

            def record_observation(boundary, **kwargs):
                observations.append(
                    (boundary, kwargs["integration_active"], monotonic.value)
                )
                return original_observe(boundary, **kwargs)

            def cross_budget(*args, **kwargs):
                monotonic.advance(2)
                return original_verify(*args, **kwargs)

            with patch.object(
                scheduler.experiment_controller,
                "observe_boundary",
                side_effect=record_observation,
            ), patch.object(
                two_phase_scheduler_module,
                "load_experiment_controller",
                return_value=scheduler.experiment_controller,
            ), patch.object(
                two_phase_scheduler_module,
                "run_integration_verification",
                side_effect=cross_budget,
            ):
                collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(result["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_baseline_commit_status"], "committed")
            self.assertNotEqual(self._head(baseline), source_head)
            self.assertTrue((baseline / "feature.txt").is_file())
            integration_observations = [
                item
                for item in observations
                if item[0] != "pre_provider_launch"
            ]
            self.assertEqual(
                [
                    (boundary, active)
                    for boundary, active, _now
                    in integration_observations
                ],
                [
                    ("pre_integration", False),
                    ("post_integration", False),
                    ("post_integration", False),
                ],
            )
            self.assertGreaterEqual(
                integration_observations[0][2],
                initial_monotonic,
            )
            self.assertGreaterEqual(
                integration_observations[1][2],
                integration_observations[0][2] + 2,
            )
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")
            self.assertFalse(scheduler.state["integration_active"])

    def test_budget_stop_is_deferred_through_integration_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            source_head = self._head(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(
                output_dir,
                monotonic,
                max_wall_time_seconds=1,
            )
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "raise SystemExit(7)"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "must roll back\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            original_verify = two_phase_scheduler_module.run_integration_verification

            def cross_budget(*args, **kwargs):
                monotonic.advance(2)
                return original_verify(*args, **kwargs)

            with patch.object(
                two_phase_scheduler_module,
                "run_integration_verification",
                side_effect=cross_budget,
            ):
                collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline = Path(result["integration_baseline_worktree_path"])
            event_types = [
                json.loads(line)["event_type"]
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_baseline_rollback_status"], "reset")
            self.assertEqual(self._head(baseline), source_head)
            self.assertFalse((baseline / "feature.txt").exists())
            self.assertIn("integration_blocked", event_types)
            self.assertIn("integration_baseline_commit_evaluated", event_types)
            self.assertEqual(collected["experiment_controller_status"], "budget_stopped")

    def test_tick_resumes_interrupted_controller_before_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "resume before integration\n",
                encoding="utf-8",
            )
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(1, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            invocation.finalize("completed", execution)
            self._append_result(inflight, ["feature.txt"])
            controller.interrupt()

            resumed = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
                resume_interrupted_experiment=True,
            )
            tick = resumed.tick()
            result = tick["collect"]["results"][0]

            self.assertEqual(tick["collect"]["collected_count"], 1)
            self.assertEqual(
                result["pre_integration_controller_observation"][
                    "controller_status"
                ],
                "active",
            )
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(
                result["integration_verification_status"],
                "passed",
            )
            self.assertNotEqual(
                result["completion_policy"],
                "accepted_patch_preserved_budget_stop",
            )

    def test_completed_integration_recovery_does_not_repeat_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            output_dir = root / "run"
            self._init_repo(repo)
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            scheduler.dispatch_ready()
            inflight = copy.deepcopy(
                scheduler.state["inflight_attempts"][0]
            )
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "commit once\n",
                encoding="utf-8",
            )
            self._append_result(inflight, ["feature.txt"])
            first = scheduler.collect_ready_results()
            first_head = self._head(
                first["results"][0][
                    "integration_baseline_worktree_path"
                ]
            )
            self.assertEqual(len(scheduler.state["steps"]), 1)

            scheduler.state["integration_active"] = True
            scheduler.state["integration_attempt_id"] = inflight[
                "attempt_id"
            ]
            scheduler.state["inflight_attempts"] = [inflight]
            scheduler._write_state()

            recovered = self._scheduler(
                root,
                controller,
                monotonic,
                write_scope=["feature.txt"],
                project_root=repo,
                verification_command=[sys.executable, "-c", "pass"],
            )
            tick = recovered.tick()

            self.assertEqual(tick["collect"]["collected_count"], 1)
            self.assertEqual(len(recovered.state["steps"]), 1)
            self.assertEqual(recovered.state["inflight_attempts"], [])
            self.assertFalse(recovered.state["integration_active"])
            self.assertEqual(
                self._head(
                    recovered.state["integration_baseline"][
                        "integration_baseline_worktree_path"
                    ]
                ),
                first_head,
            )

    def test_interrupted_scheduler_reconciles_before_original_budget_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            monotonic = _SchedulerMonotonic()
            controller = self._controller(output_dir, monotonic)
            scheduler = self._scheduler(root, controller, monotonic)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            calls = []
            invocation = self._invocation_for_inflight(
                output_dir,
                controller,
                inflight,
                ExperimentProviderBudgetBoundaryTests._usage_stdout(4, 1),
                calls,
            )
            execution = ExperimentProviderBudgetBoundaryTests._execute(
                invocation,
                root,
            )
            frozen_budget_sha256 = controller.reference["frozen_budget_sha256"]
            controller.interrupt()

            blocked = self._scheduler(
                root,
                controller,
                monotonic,
                resume_interrupted_experiment=True,
                invocation_fence_assessor=lambda _start: {
                    "fence_status": "live_pinned",
                    "proof": "deterministic_live_worker",
                },
            )
            blocked_dispatch = blocked.dispatch_ready()
            self.assertEqual(
                blocked_dispatch["dispatch_status"],
                "invocation_reconciliation_pending",
            )
            self.assertEqual(controller.controller_status, "interrupted")

            invocation.finalize("completed", execution)
            resumed = self._scheduler(
                root,
                controller,
                monotonic,
                resume_interrupted_experiment=True,
            )
            resumed_dispatch = resumed.dispatch_ready()

            self.assertEqual(resumed_dispatch["dispatch_status"], "at_capacity")
            self.assertEqual(controller.controller_status, "active")
            self.assertEqual(controller.budget_state["total_tokens"], 5)
            self.assertEqual(
                controller.reference["frozen_budget_sha256"],
                frozen_budget_sha256,
            )
            recovered_collection = resumed.collect_ready_results()
            self.assertEqual(recovered_collection["collected_count"], 1)
            self.assertEqual(recovered_collection["inflight_count"], 0)
            alternate_root = root / "alternate-controller"
            alternate = self._controller(alternate_root, monotonic)
            with self.assertRaisesRegex(
                ExperimentControllerIntegrityError,
                "reference changed on restart",
            ):
                TwoPhaseFileScheduler(
                    root / "agent_pool.json",
                    root / "backlog.json",
                    output_dir,
                    clock=_SchedulerClock(),
                    experiment_controller_reference=alternate.reference,
                    experiment_controller_required=True,
                    experiment_controller_monotonic=monotonic,
                )


class ExperimentContractSchemaTests(unittest.TestCase):
    def test_protocol_run_binding_and_state_schemas_are_executable(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="agentteam_direct",
            repetition_index=1,
            stable_request_key="stable-123",
        )

        self.assertIs(validate_experiment_protocol(protocol), protocol)
        self.assertIs(
            validate_experiment_run_manifest(manifest, protocol),
            manifest,
        )
        self.assertEqual(
            manifest["experiment_run_id"],
            derive_experiment_run_id(
                canonical_json_sha256(protocol),
                "agentteam_direct",
                1,
                "stable-123",
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            allocation = allocate_experiment_run(
                tmp,
                protocol,
                mode="agentteam_direct",
                repetition_index=1,
                stable_request_key="stable-123",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            self.assertEqual(
                validate_experiment_run_binding(allocation["binding"]),
                allocation["binding"],
            )
            self.assertEqual(
                validate_experiment_state(allocation["state"]),
                allocation["state"],
            )

    def test_protocol_requires_complete_three_mode_equal_input_contract(self):
        cases = {
            "missing-mode": lambda value: value["modes"].pop(),
            "duplicate-order": lambda value: value["mode_order"].__setitem__(
                0,
                value["mode_order"][1],
            ),
            "concurrent-provider-lanes": lambda value: value[
                "environment"
            ].__setitem__("max_inflight_model_invocations", 2),
            "missing-environment": lambda value: value["environment"].pop(
                "network_policy"
            ),
            "shell-acceptance": lambda value: value["acceptance"].__setitem__(
                "command",
                "python3 -m unittest",
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                protocol = _protocol()
                mutate(protocol)
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_protocol(protocol)

    def test_run_manifest_binds_mode_repetition_request_and_protocol(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="single_codex",
            repetition_index=0,
            stable_request_key="stable-001",
        )

        for field, value in {
            "protocol_sha256": "0" * 64,
            "mode": "agentteam_full",
            "repetition_index": 1,
            "stable_request_key": "stable-002",
        }.items():
            with self.subTest(field=field):
                drifted = dict(manifest)
                drifted[field] = value
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_run_manifest(drifted, protocol)

    def test_legacy_v1_manifest_remains_validation_only(self):
        legacy = {
            "schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
            "experiment_id": "legacy",
        }

        with self.assertRaisesRegex(ExperimentContractError, "validation-only"):
            ensure_executable_manifest(legacy)


class ExperimentAllocationTests(unittest.TestCase):
    def test_allocation_publishes_canonical_authority_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            root = Path(tmp)

            self.assertEqual(allocation["allocation_status"], "created")
            self.assertTrue(allocation["created"])
            self.assertEqual(allocation["provider_calls"], 0)
            self.assertEqual(allocation["target_mutations"], 0)
            protocol_path = Path(allocation["protocol_path"])
            manifest_path = Path(allocation["run_manifest_path"])
            binding_path = Path(allocation["run_dir"]) / "binding.json"
            state_path = Path(allocation["run_dir"]) / "state.json"
            request_path = Path(allocation["request_path"])
            for path in (
                protocol_path,
                manifest_path,
                binding_path,
                state_path,
                request_path,
            ):
                self.assertTrue(path.is_file(), path)

            self.assertEqual(
                protocol_path.read_bytes(),
                canonical_json_bytes(_protocol()) + b"\n",
            )
            self.assertEqual(
                json.loads(request_path.read_text(encoding="utf-8")),
                allocation["binding"],
            )
            self.assertFalse((Path(allocation["run_dir"]) / "repository").exists())
            self.assertEqual(allocation["state"]["status"], "prepared")
            self.assertEqual(
                allocation["binding"]["protocol_sha256"],
                canonical_json_sha256(_protocol()),
            )
            self.assertEqual(
                allocation["binding"]["run_manifest_sha256"],
                canonical_json_sha256(allocation["run_manifest"]),
            )
            self.assertEqual(
                sorted(path.name for path in (root / "protocols").iterdir()),
                [f"{allocation['protocol_sha256']}.json"],
            )

    def test_same_stable_request_returns_existing_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp)
            provider = Mock(name="provider")

            repeated = _allocate(tmp)
            if repeated["created"]:
                provider()

            self.assertEqual(repeated["allocation_status"], "existing")
            self.assertFalse(repeated["created"])
            self.assertEqual(repeated["experiment_run_id"], first["experiment_run_id"])
            self.assertEqual(repeated["binding"], first["binding"])
            self.assertEqual(repeated["provider_calls"], 0)
            provider.assert_not_called()
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 1)
            self.assertEqual(len(list((Path(tmp) / "requests").iterdir())), 1)

    def test_resume_reads_published_protocol_not_mutable_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "mutable-input.json"
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            allocation = allocate_experiment_run(
                root / "experiment-state",
                protocol_path,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="request-from-path",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            protocol_path.write_text('{"tampered":true}\n', encoding="utf-8")

            accepted = validate_resume_binding(
                allocation["run_dir"],
                runtime_release=_release(),
                repository=_protocol()["repository"],
            )

            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["protocol"], _protocol())

    def test_request_drift_fails_before_provider_or_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target-repository"
            target.mkdir()
            marker = target / "marker.txt"
            marker.write_text("unchanged\n", encoding="utf-8")
            _allocate(root)
            protocol_count = len(list((root / "protocols").iterdir()))
            provider = Mock(name="provider")
            drifted = _protocol()
            drifted["seed"] += 1

            with self.assertRaises(ExperimentContractError):
                result = allocate_experiment_run(
                    root,
                    drifted,
                    mode="single_codex",
                    repetition_index=0,
                    stable_request_key="request-001",
                    runtime_release=_release(),
                )
                if result["created"]:
                    provider()

            provider.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(len(list((root / "protocols").iterdir())), protocol_count)

    def test_distinct_request_keys_allocate_collision_safe_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp, "request-a")
            second = _allocate(tmp, "request-b")

            self.assertNotEqual(first["experiment_run_id"], second["experiment_run_id"])
            self.assertEqual(len(first["experiment_run_id"].split("-")[-1]), 64)
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 2)

    def test_tampered_published_protocol_fails_closed_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol_path = Path(allocation["protocol_path"])
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            provider = Mock(name="provider")

            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                result = _allocate(tmp)
                if result["created"]:
                    provider()

            provider.assert_not_called()


class ExperimentLeaseAndResumeTests(unittest.TestCase):
    def test_competing_controller_lease_fails_closed_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            first = acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-a",
                lease_id="lease-a",
                acquired_at="2026-07-27T00:00:01Z",
            )
            self.addCleanup(first.release)

            with self.assertRaises(ExperimentLeaseError):
                acquire_controller_lease(
                    allocation["run_dir"],
                    controller_id="controller-b",
                    lease_id="lease-b",
                )

            first.release()
            with acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-b",
                lease_id="lease-b",
            ) as second:
                self.assertTrue(second.held)
                self.assertEqual(second.record["controller_id"], "controller-b")
            self.assertFalse(second.held)

    def test_resume_accepts_exact_binding_and_rejects_all_identity_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol = _protocol()
            repository = protocol["repository"]
            release = _release()

            accepted = validate_resume_binding(
                allocation["run_dir"],
                protocol=protocol,
                run_manifest=allocation["run_manifest"],
                runtime_release=release,
                repository=repository,
                stable_request_key="request-001",
                experiment_run_id=allocation["experiment_run_id"],
                protocol_sha256=allocation["protocol_sha256"],
                run_manifest_sha256=allocation["run_manifest_sha256"],
            )
            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["provider_calls"], 0)

            drifted_protocol = copy.deepcopy(protocol)
            drifted_protocol["seed"] += 1
            drifted_manifest = build_experiment_run_manifest(
                protocol,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="another-request",
            )
            drifted_release = _release()
            drifted_release["release_id"] = "candidate-v2"
            drifted_repository = dict(repository)
            drifted_repository["source"] = "/srv/other/repository.git"
            drifted_object_format = {
                **repository,
                "commit": "8" * 64,
                "tree": "9" * 64,
                "git_object_format": "sha256",
            }
            cases = {
                "protocol": {"protocol": drifted_protocol},
                "run-manifest": {"run_manifest": drifted_manifest},
                "release": {"runtime_release": drifted_release},
                "repository": {"repository": drifted_repository},
                "object-format": {"repository": drifted_object_format},
                "request": {"stable_request_key": "another-request"},
            }
            for name, overrides in cases.items():
                with self.subTest(name=name):
                    arguments = {
                        "protocol": protocol,
                        "run_manifest": allocation["run_manifest"],
                        "runtime_release": release,
                        "repository": repository,
                        "stable_request_key": "request-001",
                    }
                    arguments.update(overrides)
                    provider = Mock(name=f"provider-{name}")
                    with self.assertRaises(ExperimentContractError):
                        result = validate_resume_binding(
                            allocation["run_dir"],
                            **arguments,
                        )
                        if result["resume_status"] == "accepted":
                            provider()
                    provider.assert_not_called()

    def test_resume_rejects_tampered_request_binding_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            request_path = Path(allocation["request_path"])
            request_path.write_text(
                json.dumps(allocation["binding"], indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                validate_resume_binding(allocation["run_dir"])

        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            state_path = Path(allocation["run_dir"]) / "state.json"
            state = dict(allocation["state"])
            state.update(
                {
                    "status": "budget_stopped",
                    "state_version": 2,
                    "updated_at": "2026-07-27T00:00:02Z",
                }
            )
            state_path.write_bytes(canonical_json_bytes(state) + b"\n")

            with self.assertRaisesRegex(ExperimentContractError, "cannot resume"):
                validate_resume_binding(allocation["run_dir"])


class ExperimentWorkspaceTests(unittest.TestCase):
    def test_allocates_independent_exact_commit_snapshot_and_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-fixture"
            run_dir.mkdir()

            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
                attested_at="2026-07-27T00:00:03Z",
            )

            snapshot = Path(allocation["snapshot_path"])
            attestation = load_clean_snapshot_attestation(
                allocation["attestation_path"]
            )
            self.assertEqual(
                validate_clean_snapshot_attestation(attestation),
                attestation,
            )
            self.assertEqual(
                _git(snapshot, "rev-parse", "HEAD").stdout.strip(),
                fixture["repository"]["commit"],
            )
            self.assertEqual(
                _git(snapshot, "rev-parse", "HEAD^{tree}").stdout.strip(),
                fixture["repository"]["tree"],
            )
            self.assertNotEqual(
                Path(attestation["snapshot_common_dir"]),
                Path(attestation["source_common_dir"]),
            )
            self.assertTrue(Path(attestation["snapshot_common_dir"]).is_dir())
            self.assertTrue(attestation["detached_head"])
            self.assertTrue(attestation["worktree_clean"])
            self.assertEqual(attestation["remotes"], [])
            self.assertEqual(attestation["alternates"], [])
            self.assertEqual(attestation["extra_refs"], [])
            self.assertFalse((snapshot / ".agentteam").exists())
            self.assertFalse((snapshot / "untracked.patch").exists())
            self.assertEqual(
                _git(snapshot, "symbolic-ref", "-q", "HEAD", check=False).returncode,
                1,
            )
            self.assertEqual(_git(snapshot, "remote").stdout, "")
            self.assertEqual(
                _git(
                    snapshot,
                    "for-each-ref",
                    "--format=%(refname)",
                ).stdout,
                "",
            )
            self.assertNotEqual(
                _git(
                    snapshot,
                    "cat-file",
                    "-e",
                    fixture["parent_commit"],
                    check=False,
                ).returncode,
                0,
            )

    def test_sha256_object_format_snapshot_preserves_exact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp, object_format="sha256")
            run_dir = Path(tmp) / "experiment-run-sha256"
            run_dir.mkdir()

            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )

            snapshot = Path(allocation["snapshot_path"])
            self.assertEqual(
                allocation["attestation"]["git_object_format"],
                "sha256",
            )
            self.assertEqual(
                len(allocation["attestation"]["head_commit"]),
                64,
            )
            self.assertEqual(
                _git(
                    snapshot,
                    "rev-parse",
                    "--show-object-format",
                ).stdout.strip(),
                "sha256",
            )
            self.assertNotEqual(
                _git(
                    snapshot,
                    "cat-file",
                    "-e",
                    fixture["parent_commit"],
                    check=False,
                ).returncode,
                0,
            )

    def test_exact_tree_and_bounded_inventory_fail_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            cases = {
                "tree-mismatch": {
                    "repository": {
                        **fixture["repository"],
                        "tree": "0" * 40,
                    },
                    "inventory_limit": 100,
                },
                "inventory-overflow": {
                    "repository": fixture["repository"],
                    "inventory_limit": 1,
                },
            }
            for name, case in cases.items():
                with self.subTest(name=name):
                    run_dir = Path(tmp) / f"experiment-run-{name}"
                    run_dir.mkdir()
                    with self.assertRaises(ExperimentWorkspaceError):
                        allocate_clean_snapshot(
                            run_dir,
                            case["repository"],
                            inventory_limit=case["inventory_limit"],
                        )
                    self.assertFalse((run_dir / "repository").exists())
                    self.assertFalse((run_dir / "clean-snapshot.json").exists())
                    self.assertEqual(
                        list(run_dir.glob(".repository-staging-*")),
                        [],
                    )

            preexisting_run = Path(tmp) / "experiment-run-preexisting"
            preexisting_snapshot = preexisting_run / "repository"
            preexisting_snapshot.mkdir(parents=True)
            marker = preexisting_snapshot / "operator-evidence.txt"
            marker.write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(ExperimentWorkspaceError, "already exists"):
                allocate_clean_snapshot(
                    preexisting_run,
                    fixture["repository"],
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_verification_denies_remote_alternate_and_extra_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-contamination"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            snapshot = Path(allocation["snapshot_path"])

            _git(snapshot, "remote", "add", "forbidden", fixture["repository"]["source"])
            with self.assertRaisesRegex(ExperimentWorkspaceError, "remote"):
                verify_clean_snapshot(snapshot, fixture["repository"])
            _git(snapshot, "remote", "remove", "forbidden")

            _git(
                snapshot,
                "update-ref",
                "refs/heads/forbidden",
                fixture["repository"]["commit"],
            )
            with self.assertRaisesRegex(ExperimentWorkspaceError, "extra Git refs"):
                verify_clean_snapshot(snapshot, fixture["repository"])
            _git(snapshot, "update-ref", "-d", "refs/heads/forbidden")

            common_dir = Path(
                _git(
                    snapshot,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ).stdout.strip()
            )
            alternates = common_dir / "objects" / "info" / "alternates"
            alternates.write_text(
                str(fixture["source"] / ".git" / "objects") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExperimentWorkspaceError, "alternates"):
                verify_clean_snapshot(snapshot, fixture["repository"])

    def test_symlink_escape_is_rejected_without_leaving_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp, escaping_symlink=True)
            run_dir = Path(tmp) / "experiment-run-symlink"
            run_dir.mkdir()

            with self.assertRaisesRegex(ExperimentWorkspaceError, "symlinks"):
                allocate_clean_snapshot(run_dir, fixture["repository"])

            self.assertFalse((run_dir / "repository").exists())
            self.assertFalse((run_dir / "clean-snapshot.json").exists())
            self.assertEqual(list(run_dir.glob(".repository-staging-*")), [])

    def test_cleanup_preserves_sealed_result_and_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-cleanup"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            result_dir = run_dir / "results" / "sealed-result"
            result_dir.mkdir(parents=True)
            result_path = result_dir / "result.json"
            result_path.write_text('{"status":"completed"}\n', encoding="utf-8")
            unsafe_result = (
                Path(allocation["snapshot_path"]) / "provider-result.json"
            )
            unsafe_result.write_text('{"unsafe":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentWorkspaceError,
                "outside the disposable snapshot",
            ):
                cleanup_clean_snapshot(
                    run_dir,
                    sealed_result_path=unsafe_result,
                )
            self.assertTrue(Path(allocation["snapshot_path"]).is_dir())
            unsafe_result.unlink()

            cleanup = cleanup_clean_snapshot(
                run_dir,
                sealed_result_path=result_dir,
            )

            self.assertEqual(cleanup["cleanup_status"], "removed")
            self.assertTrue(cleanup["result_preserved"])
            self.assertFalse(Path(allocation["snapshot_path"]).exists())
            self.assertEqual(
                result_path.read_text(encoding="utf-8"),
                '{"status":"completed"}\n',
            )
            self.assertEqual(
                cleanup["sealed_result_sha256"],
                cleanup["sealed_result_sha256_after_cleanup"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = _fixture_repository(tmp)
            run_dir = Path(tmp) / "experiment-run-cleanup-failure"
            run_dir.mkdir()
            allocation = allocate_clean_snapshot(
                run_dir,
                fixture["repository"],
            )
            result_path = run_dir / "sealed-result.json"
            result_path.write_text('{"status":"failed"}\n', encoding="utf-8")

            with patch(
                "agentteam_runtime.experiment_workspace.shutil.rmtree",
                side_effect=OSError("simulated cleanup failure"),
            ):
                cleanup = cleanup_clean_snapshot(
                    run_dir,
                    sealed_result_path=result_path,
                )

            self.assertEqual(cleanup["cleanup_status"], "failed")
            self.assertIn("simulated cleanup failure", cleanup["error"])
            self.assertTrue(Path(allocation["snapshot_path"]).is_dir())
            self.assertEqual(
                result_path.read_text(encoding="utf-8"),
                '{"status":"failed"}\n',
            )


class ExperimentSandboxTests(unittest.TestCase):
    def test_evaluator_execution_uses_digest_bound_memory_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            evaluator = Path(tmp) / "evaluator.py"
            original = b"#!/usr/bin/python3\nraise SystemExit(0)\n"
            evaluator.write_bytes(original)
            evaluator.chmod(0o700)
            digest = hashlib.sha256(original).hexdigest()

            content = _read_digest_bound_evaluator(
                evaluator,
                digest,
            )
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(91)\n",
                encoding="utf-8",
            )
            self.assertEqual(content, original)

    def test_candidate_repository_must_descend_from_certified_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            repository = fixture["repository"]
            baseline = fixture["repository_identity"]
            state = _candidate_repository_state(repository, baseline)
            self.assertEqual(state["baseline_commit"], baseline["commit"])
            fsmonitor_marker = Path(tmp) / "fsmonitor-executed"
            fsmonitor = Path(tmp) / "fsmonitor.sh"
            fsmonitor.write_text(
                "#!/bin/sh\n"
                f"touch {str(fsmonitor_marker)!r}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fsmonitor.chmod(0o700)
            _git(repository, "config", "core.fsmonitor", str(fsmonitor))
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "Git config contains undeclared behavior",
            ):
                _candidate_repository_state(repository, baseline)
            self.assertFalse(fsmonitor_marker.exists())
            _git(repository, "config", "--unset", "core.fsmonitor")
            tracked = repository / "tracked.txt"
            tracked.write_text("first dirty value\n", encoding="utf-8")
            first_dirty = _candidate_repository_state(repository, baseline)
            tracked.write_text("second dirty value\n", encoding="utf-8")
            second_dirty = _candidate_repository_state(repository, baseline)
            self.assertEqual(
                first_dirty["tracked_status_sha256"],
                second_dirty["tracked_status_sha256"],
            )
            self.assertNotEqual(
                first_dirty["working_tree_sha256"],
                second_dirty["working_tree_sha256"],
            )
            (repository / "untracked.txt").write_text(
                "untracked\n",
                encoding="utf-8",
            )
            with_untracked = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                second_dirty["working_tree_sha256"],
                with_untracked["working_tree_sha256"],
            )
            (repository / "untracked.txt").chmod(0o755)
            executable_untracked = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_untracked["working_tree_sha256"],
                executable_untracked["working_tree_sha256"],
            )
            (repository / ".git" / "info" / "exclude").write_text(
                "ignored.txt\n",
                encoding="utf-8",
            )
            (repository / "ignored.txt").write_text(
                "ignored content\n",
                encoding="utf-8",
            )
            with_ignored = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                executable_untracked["working_tree_sha256"],
                with_ignored["working_tree_sha256"],
            )
            empty_directory = repository / "empty-directory"
            empty_directory.mkdir()
            with_empty_directory = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["working_tree_sha256"],
                with_empty_directory["working_tree_sha256"],
            )
            empty_directory.rmdir()
            git_config = repository / ".git" / "config"
            original_git_config = git_config.read_bytes()
            _git(repository, "config", "user.name", "Changed Safe Name")
            with_git_config = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["git_control_sha256"],
                with_git_config["git_control_sha256"],
            )
            git_config.write_bytes(original_git_config)
            hook = repository / ".git" / "hooks" / "post-commit"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook.chmod(0o700)
            with_git_hook = _candidate_repository_state(
                repository,
                baseline,
            )
            self.assertNotEqual(
                with_ignored["git_control_sha256"],
                with_git_hook["git_control_sha256"],
            )
            hook.unlink()
            _git(repository, "reset", "--quiet", "--hard", baseline["commit"])
            (repository / "untracked.txt").unlink()
            (repository / "ignored.txt").unlink()

            _git(repository, "checkout", "--quiet", "--orphan", "unrelated")
            _git(repository, "rm", "--quiet", "-rf", ".")
            (repository / "unrelated.txt").write_text(
                "unrelated\n",
                encoding="utf-8",
            )
            _git(repository, "add", "unrelated.txt")
            _git(repository, "commit", "--quiet", "-m", "unrelated")
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "does not descend",
            ):
                _candidate_repository_state(repository, baseline)

    def test_candidate_repository_requires_standalone_git_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            linked = root / "linked-worktree"
            _git(
                fixture["repository"],
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(linked),
                fixture["repository_identity"]["commit"],
            )
            self.assertTrue((linked / ".git").is_file())
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "candidate Git control directory must be a directory",
            ):
                _candidate_repository_state(
                    linked,
                    fixture["repository_identity"],
                )

    def test_provider_descriptor_rejects_non_system_bubblewrap_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            fake_bwrap = Path(tmp) / "bwrap"
            shutil.copy2("/usr/bin/bwrap", fake_bwrap)
            fake_bwrap.chmod(0o700)
            forged = copy.deepcopy(fixture["descriptor"])
            forged["bwrap_path"] = str(fake_bwrap)
            forged["bwrap_sha256"] = hashlib.sha256(
                fake_bwrap.read_bytes()
            ).hexdigest()
            forged["policy_sha256"] = _sandbox_policy_sha256(forged)

            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "identity is unavailable or changed",
            ):
                validate_provider_sandbox_descriptor(forged)

    def test_provider_namespace_has_only_declared_views_and_bounded_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            descriptor = fixture["descriptor"]

            serialized = json.dumps(descriptor, sort_keys=True)
            self.assertNotIn(str(fixture["canary"]), serialized)
            self.assertNotIn(
                fixture["canary"].read_text(encoding="utf-8"),
                serialized,
            )
            self.assertEqual(len(descriptor["credential_views"]), 1)
            credential = descriptor["credential_views"][0]
            self.assertFalse(credential["writable"])
            self.assertEqual(credential["file_count"], 1)
            self.assertLessEqual(credential["total_bytes"], 1024 * 1024)

            prepared = prepare_provider_launch(
                descriptor,
                [str(Path(sys.executable).resolve()), "-c", "print('provider')"],
                cwd=fixture["repository"],
            )
            command = list(prepared.command)
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("--bind", command)
            self.assertIn("--ro-bind", command)
            self.assertEqual(prepared.cwd, "/")
            self.assertEqual(
                prepared.environment["AGENTTEAM_CREDENTIAL_FILE"],
                "/run/agentteam-credentials/provider.json",
            )
            self.assertNotIn(str(fixture["canary"]), " ".join(command))

            fake_python = Path(tmp) / "python3"
            fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_python.chmod(0o700)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "targets must not overlap",
            ):
                build_provider_sandbox_descriptor(
                    fixture["repository"],
                    runtime_views=[
                        {"source": "/usr", "target": "/usr"},
                        {
                            "source": str(fake_python),
                            "target": "/usr/bin/python3",
                        },
                    ],
                    bwrap_path="/usr/bin/bwrap",
                    forbidden_paths=[fixture["canary"]],
                )
            shadowing = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": "/usr", "target": "/runtime-usr"},
                    {
                        "source": str(fake_python),
                        "target": "/usr/bin/python3",
                    },
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "executable is not approved",
            ):
                _approved_acceptance_executable(
                    "/usr/bin/python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=shadowing,
                )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "must be an absolute path",
            ):
                _approved_acceptance_executable(
                    "python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=fixture["descriptor"],
                )
            symlink_runtime = Path(tmp) / "symlink-runtime"
            (symlink_runtime / "bin").mkdir(parents=True)
            os.symlink(
                "/runtime-usr/bin/python3.12",
                symlink_runtime / "bin" / "python3",
            )
            symlink_shadowing = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": "/usr", "target": "/runtime-usr"},
                    {"source": str(symlink_runtime), "target": "/usr"},
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "contains a symlink",
            ):
                _approved_acceptance_executable(
                    "/usr/bin/python3",
                    cwd=fixture["repository"],
                    environment={"PATH": "/usr/bin:/bin"},
                    descriptor=symlink_shadowing,
                )
            drift_source = Path(tmp) / "runtime-drift"
            drift_source.mkdir()
            drift_descriptor = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {"source": str(drift_source), "target": "/runtime-drift"},
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            original_source = Path(tmp) / "runtime-drift-original"
            drift_source.rename(original_source)
            os.symlink(str(Path(tmp)), drift_source)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "canonical non-symlink path",
            ):
                validate_provider_sandbox_descriptor(
                    drift_descriptor,
                    require_namespace_evidence=False,
                )
            mutable_runtime = Path(tmp) / "mutable-runtime"
            mutable_runtime.mkdir()
            mutable_tool = mutable_runtime / "tool"
            mutable_tool.write_text("first\n", encoding="utf-8")
            mutable_descriptor = build_provider_sandbox_descriptor(
                fixture["repository"],
                runtime_views=[
                    {
                        "source": str(mutable_runtime),
                        "target": "/mutable-runtime",
                    },
                ],
                bwrap_path="/usr/bin/bwrap",
                forbidden_paths=[fixture["canary"]],
            )
            mutable_tool.write_text("second\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "identity is unavailable or changed",
            ):
                validate_provider_sandbox_descriptor(
                    mutable_descriptor,
                    require_namespace_evidence=False,
                )

    def test_evaluator_mount_or_inconclusive_probe_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "evaluator-only path overlaps",
            ):
                build_provider_sandbox_descriptor(
                    fixture["repository"],
                    runtime_views=[fixture["canary"].parent],
                    bwrap_path=fixture["descriptor"]["bwrap_path"],
                    forbidden_paths=[fixture["canary"]],
                )

            completed = subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    '{"content_readable":false,"path_visible":false}'
                ),
                stderr="",
            )
            probe = probe_gold_canary_denial(
                fixture["uncertified_descriptor"],
                fixture["canary"],
                runner=Mock(return_value=completed),
                probe_python=str(Path(sys.executable).resolve()),
            )
            self.assertEqual(probe["denial_status"], "denied")
            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "inconclusive",
            ):
                probe_gold_canary_denial(
                    fixture["uncertified_descriptor"],
                    fixture["canary"],
                    runner=Mock(
                        return_value=subprocess.CompletedProcess(
                            [],
                            0,
                            stdout="not-json",
                            stderr="",
                        )
                    ),
                    probe_python=str(Path(sys.executable).resolve()),
                )

    def test_real_bwrap_canary_denial_when_runner_supports_namespaces(self):
        bwrap = shutil.which("bwrap")
        if not bwrap or not sys.platform.startswith("linux"):
            self.skipTest("real bubblewrap is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            canary = root / "evaluator-only" / "gold-canary"
            canary.parent.mkdir()
            canary.write_text("real-bwrap-canary", encoding="utf-8")
            views = [
                {"source": path, "target": path}
                for path in ("/usr", "/lib", "/lib64", "/bin")
                if Path(path).exists()
            ]
            descriptor = build_provider_sandbox_descriptor(
                repository,
                runtime_views=views,
                bwrap_path=bwrap,
                forbidden_paths=[canary],
            )
            try:
                evidence = probe_gold_canary_denial(
                    descriptor,
                    canary,
                    probe_python="/usr/bin/python3",
                )
            except ExperimentSandboxUnavailable:
                if os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") == "1":
                    raise
                self.skipTest("runner disallows unprivileged bubblewrap")
            self.assertEqual(evidence["denial_status"], "denied")
            self.assertFalse(evidence["path_visible"])
            self.assertFalse(evidence["content_readable"])

    def test_real_systemd_evaluator_contains_detached_child_when_required(self):
        if os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1":
            self.skipTest("real systemd evaluator probe is opt-in")
        if not shutil.which("systemd-run") or not shutil.which("systemctl"):
            self.fail("systemd evaluator probe was required but is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_environment = {
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "/tmp",
            }
            script = (
                "import json,os,pathlib,subprocess\n"
                "child=subprocess.Popen(['/bin/sleep','60'],"
                "start_new_session=True)\n"
                "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                "pathlib.Path('environment.json').write_text("
                "json.dumps(dict(os.environ),sort_keys=True))\n"
            )
            execution = _run_bounded_argv(
                [str(Path(sys.executable).resolve()), "-c", script],
                cwd=root,
                environment=expected_environment,
                timeout_seconds=10,
                max_output_bytes=4096,
                cpu_limit=1,
                memory_limit_bytes=128 * 1024 * 1024,
            )
            child_pid = int((root / "child.pid").read_text(encoding="utf-8"))
            time.sleep(0.1)
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_alive = False
            else:
                child_alive = True
                os.kill(child_pid, 9)
            self.assertFalse(child_alive)
            self.assertEqual(
                json.loads(
                    (root / "environment.json").read_text(encoding="utf-8")
                ),
                expected_environment,
            )
            self.assertEqual(
                execution["execution_boundary"],
                "systemd_user_transient_service",
            )
            self.assertTrue(execution["systemd_unit"].endswith(".service"))

    def test_real_systemd_bwrap_contains_detached_candidate_when_required(self):
        if (
            os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1"
            or os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") != "1"
        ):
            self.skipTest("real systemd plus bwrap probe is opt-in")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            evaluator = root / "contract-evaluator.py"
            evaluator_content = (
                b"#!/usr/bin/python3\n"
                b"import sys\n"
                b"raise SystemExit(0 if len(sys.argv) >= 3 "
                b"and sys.argv[1] == '--' else 64)\n"
            )
            evaluator.write_bytes(evaluator_content)
            evaluator.chmod(0o700)
            child_started_path = fixture["repository"] / "child-started"
            child_survived_path = fixture["repository"] / "child-survived"
            git_writable_path = fixture["repository"] / "git-writable"
            git_readonly_path = fixture["repository"] / "git-readonly"
            git_probe_path = fixture["repository"] / ".git" / "write-probe"
            child_script = (
                "import pathlib,time;"
                f"pathlib.Path({str(child_started_path)!r}).write_text('1');"
                "time.sleep(4);"
                f"pathlib.Path({str(child_survived_path)!r}).write_text('1')"
            )
            acceptance = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "import pathlib,subprocess,time;"
                    "\ntry:\n"
                    f" pathlib.Path({str(git_probe_path)!r}).write_text('1')\n"
                    "except OSError:\n"
                    f" pathlib.Path({str(git_readonly_path)!r}).write_text('1')\n"
                    "else:\n"
                    f" pathlib.Path({str(git_writable_path)!r}).write_text('1')\n"
                    "child=subprocess.Popen("
                    f"[{str(Path(sys.executable).resolve())!r},"
                    f"'-c',{child_script!r}],"
                    "start_new_session=True);"
                    f"pathlib.Path({str(child_started_path)!r})."
                    "write_text(str(child.pid));"
                    "time.sleep(60)"
                ),
            ]
            prepared = prepare_candidate_evaluation_launch(
                fixture["descriptor"],
                evaluator,
                hashlib.sha256(evaluator_content).hexdigest(),
                acceptance,
                cwd=fixture["repository"],
            )
            try:
                execution = _run_bounded_argv(
                    list(prepared.command),
                    cwd=prepared.cwd,
                    environment=prepared.environment,
                    timeout_seconds=2,
                    max_output_bytes=4096,
                    cpu_limit=1,
                    memory_limit_bytes=128 * 1024 * 1024,
                    input_bytes=evaluator_content,
                )
            except ExperimentSandboxUnavailable:
                self.fail("required real systemd plus bwrap probe is unavailable")
            self.assertTrue(execution["timed_out"], execution)
            self.assertTrue(child_started_path.is_file())
            self.assertTrue(git_readonly_path.is_file())
            self.assertFalse(git_writable_path.exists())
            time.sleep(2.5)
            self.assertFalse(child_survived_path.exists())

    def test_real_bwrap_main_exit_does_not_leave_child_when_required(self):
        if (
            os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR") != "1"
            or os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") != "1"
        ):
            self.skipTest("real systemd plus bwrap probe is opt-in")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            evaluator = root / "contract-evaluator.py"
            evaluator_content = b"#!/usr/bin/python3\nraise SystemExit(0)\n"
            evaluator.write_bytes(evaluator_content)
            evaluator.chmod(0o700)
            survived_path = fixture["repository"] / "detached-survived"
            child_script = (
                "import pathlib,time;"
                "time.sleep(2);"
                f"pathlib.Path({str(survived_path)!r}).write_text('1')"
            )
            acceptance = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "import subprocess;"
                    "subprocess.Popen("
                    f"[{str(Path(sys.executable).resolve())!r},"
                    f"'-c',{child_script!r}],"
                    "start_new_session=True)"
                ),
            ]
            prepared = prepare_candidate_evaluation_launch(
                fixture["descriptor"],
                evaluator,
                hashlib.sha256(evaluator_content).hexdigest(),
                acceptance,
                cwd=fixture["repository"],
            )
            execution = _run_bounded_argv(
                list(prepared.command),
                cwd=prepared.cwd,
                environment=prepared.environment,
                timeout_seconds=5,
                max_output_bytes=4096,
                cpu_limit=1,
                memory_limit_bytes=128 * 1024 * 1024,
                input_bytes=evaluator_content,
            )
            self.assertFalse(execution["timed_out"], execution)
            self.assertEqual(execution["returncode"], 0, execution)
            time.sleep(2.5)
            self.assertFalse(survived_path.exists())

    def test_provider_environment_rejects_canary_content_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            canary = root / "evaluator-only" / "gold-canary"
            canary.parent.mkdir()
            canary.write_text("provider-must-not-see-this", encoding="utf-8")
            digest = hashlib.sha256(canary.read_bytes()).hexdigest()
            bwrap = Path("/usr/bin/bwrap")
            for leaked_value in (canary.read_text(encoding="utf-8"), digest):
                with self.subTest(leaked_value=leaked_value):
                    with self.assertRaisesRegex(
                        ExperimentSandboxError,
                        "evaluator-only material",
                    ):
                        build_provider_sandbox_descriptor(
                            repository,
                            runtime_views=[Path(sys.executable).resolve()],
                            environment={"LEAKED_GOLD": leaked_value},
                            bwrap_path=bwrap,
                            forbidden_paths=[canary],
                        )

    def test_common_model_invocation_policy_wraps_supported_and_fake_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            captures = []

            class FakeGatedRunner:
                def __init__(
                    self,
                    lifecycle,
                    command,
                    *,
                    cwd,
                    input_text,
                    timeout_seconds,
                    environment,
                ):
                    captures.append(
                        {
                            "command": command,
                            "cwd": cwd,
                            "environment": environment,
                        }
                    )

                def prepare(self):
                    return ExecutionGroupIdentity.not_applicable()

                def permit_and_wait(self, **_kwargs):
                    return ProviderExecution([], 0, "", "")

                def abort_before_permit(self):
                    return None

                def cleanup_after_terminal(self):
                    return None

            supported_root = Path(tmp) / "supported-authority"
            supported_root.mkdir()
            supported_lifecycle_root = experiment_lifecycle_authority_root(
                supported_root,
                "supported",
            )
            supported_reference = _publish_test_sandbox_reference(
                supported_root,
                fixture,
            )
            supported_context = _model_context(
                supported=True,
                sandbox_reference=supported_reference,
            )
            supported_context["experiment_authority_root"] = str(
                supported_root
            )
            supported = ModelInvocationCall(
                supported_lifecycle_root,
                supported_context,
                supported=True,
                systemd_runner_factory=FakeGatedRunner,
            )
            supported.execute(
                [str(Path(sys.executable).resolve()), "-c", "print('supported')"],
                cwd=fixture["repository"],
                input_text="prompt",
                timeout_seconds=10,
            )
            self.assertTrue(supported.lifecycle.started_path.is_file())
            start_record = json.loads(
                supported.lifecycle.started_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                start_record["experiment_sandbox_policy_sha256"],
                fixture["descriptor"]["policy_sha256"],
            )
            self.assertEqual(
                start_record["experiment_sandbox_reference_sha256"],
                supported_reference["sha256"],
            )
            self.assertEqual(captures[0]["cwd"], "/")
            self.assertEqual(
                captures[0]["command"][0],
                fixture["descriptor"]["bwrap_path"],
            )
            self.assertEqual(
                captures[0]["environment"],
                fixture["descriptor"]["environment"],
            )

            fake_root = Path(tmp) / "fake-authority"
            fake_root.mkdir()
            fake_lifecycle_root = experiment_lifecycle_authority_root(
                fake_root,
                "fake",
            )
            fake_reference = _publish_test_sandbox_reference(
                fake_root,
                fixture,
            )
            fake_context = _model_context(
                supported=False,
                sandbox_reference=fake_reference,
            )
            fake_context["experiment_authority_root"] = str(fake_root)
            fake = ModelInvocationCall(
                fake_lifecycle_root,
                fake_context,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation._run_bounded_process",
                return_value=ProviderExecution([], 0, "", ""),
            ) as bounded:
                fake.execute(
                    [str(Path(sys.executable).resolve()), "-c", "print('fake')"],
                    cwd=fixture["repository"],
                    input_text="prompt",
                    timeout_seconds=10,
                )
            self.assertEqual(
                bounded.call_args.args[0][0],
                fixture["descriptor"]["bwrap_path"],
            )
            self.assertEqual(
                bounded.call_args.kwargs["environment"],
                fixture["descriptor"]["environment"],
            )

    def test_sandbox_publication_requires_fresh_probe_and_valid_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            tampered_environment = copy.deepcopy(fixture["descriptor"])
            tampered_environment["environment"]["UNDECLARED_SECRET"] = "unsafe"
            authority = Path(tmp) / "authority-valid"
            authority.mkdir()
            reference = _publish_test_sandbox_reference(authority, fixture)
            self.assertTrue(Path(reference["path"]).is_file())

            invalid_authority = Path(tmp) / "authority-invalid"
            invalid_authority.mkdir()
            with self.assertRaises(ExperimentSandboxError):
                publish_provider_sandbox_reference(
                    invalid_authority,
                    tampered_environment,
                    fixture["canary"],
                )

    def test_required_experiment_sandbox_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = _model_context(
                supported=False,
                sandbox_reference=None,
            )
            context["experiment_sandbox_required"] = True
            invocation = ModelInvocationCall(
                Path(tmp) / "authority",
                context,
                supported=False,
            )
            with patch(
                "agentteam_runtime.model_invocation.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ModelInvocationIntegrityError,
                    "sandbox is required",
                ):
                    invocation.execute(
                        [str(Path(sys.executable).resolve()), "-c", "print('unsafe')"],
                        cwd=tmp,
                        input_text="prompt",
                        timeout_seconds=10,
                    )
            popen.assert_not_called()

    def test_sandbox_authority_must_be_explicit_and_provider_invisible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            authority = root / "authority"
            authority.mkdir()
            reference = _publish_test_sandbox_reference(
                authority,
                fixture,
            )
            missing_authority = _model_context(
                supported=False,
                sandbox_reference=reference,
            )
            invocation = ModelInvocationCall(
                authority / "missing-authority-output",
                missing_authority,
                supported=False,
            )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "authority root is required",
            ):
                invocation.execute(
                    [str(Path(sys.executable).resolve()), "-c", "pass"],
                    cwd=fixture["repository"],
                    input_text="",
                    timeout_seconds=10,
                )

            visible_authority = fixture["repository"] / "visible-authority"
            visible_authority.mkdir()
            visible_lifecycle_root = experiment_lifecycle_authority_root(
                visible_authority,
                "visible",
            )
            visible_reference = _publish_test_sandbox_reference(
                visible_authority,
                fixture,
            )
            visible_context = _model_context(
                supported=False,
                sandbox_reference=visible_reference,
            )
            visible_context["experiment_authority_root"] = str(
                visible_authority
            )
            visible = ModelInvocationCall(
                visible_lifecycle_root,
                visible_context,
                supported=False,
            )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "authority root is provider-visible",
            ):
                visible.execute(
                    [str(Path(sys.executable).resolve()), "-c", "pass"],
                    cwd=fixture["repository"],
                    input_text="",
                    timeout_seconds=10,
                )

    def test_sandbox_policy_survives_mailbox_context_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            reference = _publish_test_sandbox_reference(
                tmp,
                fixture,
            )
            message = {
                "payload": {
                    "model_invocation_context": {
                        "experiment_sandbox_reference": reference,
                        "experiment_sandbox_required": True,
                        "experiment_authority_root": tmp,
                        "experiment_controller_reference": {
                            "schema_version": (
                                "experiment_budget_controller_reference.v1"
                            )
                        },
                        "experiment_controller_required": True,
                    }
                }
            }
            projected = _model_invocation_context_payload(message)
            context = invocation_context_from_message(
                {"payload": projected},
                backend="codex",
            )
            self.assertTrue(context["experiment_sandbox_required"])
            self.assertEqual(
                context["experiment_sandbox_reference"],
                reference,
            )
            self.assertEqual(context["experiment_authority_root"], tmp)
            self.assertTrue(context["experiment_controller_required"])
            self.assertEqual(
                context["experiment_controller_reference"],
                {
                    "schema_version": (
                        "experiment_budget_controller_reference.v1"
                    )
                },
            )

    def test_trusted_argv_evaluation_waits_for_terminal_and_avoids_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner_patch = patch(
                "agentteam_runtime.experiment_sandbox._run_bounded_argv",
                side_effect=_test_evaluator_execution,
            )
            runner_patch.start()
            self.addCleanup(runner_patch.stop)
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            sandbox_reference = _publish_test_sandbox_reference(
                root,
                fixture,
            )
            lifecycle_authority_root = (
                experiment_lifecycle_authority_root(
                    root,
                    "fixture",
                )
            )
            lifecycle_context = _model_context(
                supported=False,
                sandbox_reference=None,
            )
            lifecycle_context["experiment_sandbox_policy_sha256"] = fixture[
                "descriptor"
            ]["policy_sha256"]
            lifecycle_context["experiment_sandbox_reference_sha256"] = (
                sandbox_reference["sha256"]
            )
            lifecycle = InvocationLifecycle(
                lifecycle_authority_root,
                lifecycle_context,
                invocation_id="INV-FIXTURE",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            evaluator = root / "trusted-evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\n"
                "import sys\n"
                "if len(sys.argv) < 3 or sys.argv[1] != '--':\n"
                "    raise SystemExit(64)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            evaluator_reference = publish_evaluator_reference(root, evaluator)
            evaluator_digest = evaluator_reference["sha256"]
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(91)\n",
                encoding="utf-8",
            )
            prompt = root / "prompt.txt"
            context = root / "context.json"
            taskpack = root / "taskpack"
            artifacts = root / "artifacts"
            prompt.write_text("safe prompt\n", encoding="utf-8")
            context.write_text("{}\n", encoding="utf-8")
            taskpack.mkdir()
            artifacts.mkdir()
            (taskpack / "task.json").write_text("{}\n", encoding="utf-8")
            scan_groups = {
                "prompt": [prompt],
                "context": [context],
                "taskpack": [taskpack],
                "artifacts": [artifacts],
            }
            scan_scope_reference = publish_scan_scope_reference(
                root,
                scan_groups,
            )
            invocation_set_reference = (
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": ["INV-FIXTURE"],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                )
            )
            marker = root / "shell-must-not-run"
            command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "import sys; print(sys.argv[1])",
                f"literal;touch {marker}",
            ]
            protocol = _sandbox_protocol(fixture)
            protocol["acceptance"] = {
                "command": command,
                "timeout_seconds": 10,
            }
            protocol["evaluator"]["artifact_sha256"] = evaluator_digest
            protocol_reference = publish_experiment_protocol_reference(
                root,
                protocol,
                reference_id="main-protocol",
            )

            with patch(
                "agentteam_runtime.experiment_sandbox.subprocess.Popen"
            ) as popen:
                with self.assertRaisesRegex(
                    ExperimentEvaluationBlocked,
                    "must terminate",
                ):
                    run_trusted_argv_evaluator(
                        authority_root=root,
                        invocation_set_reference=invocation_set_reference,
                        provider_sandbox_reference=sandbox_reference,
                        experiment_protocol_reference=protocol_reference,
                        scan_scope_reference=scan_scope_reference,
                        command=command,
                        cwd=fixture["repository"],
                        evaluator_reference=evaluator_reference,
                        canary_path=fixture["canary"],
                        timeout_seconds=10,
                    )
            popen.assert_not_called()

            lifecycle.finalize(
                "completed",
                stdout="",
                stderr="",
            )
            evidence_path = root / "evaluation.json"
            evidence = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=command,
                cwd=fixture["repository"],
                evaluator_reference=evaluator_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
                evidence_path=evidence_path,
            )

            self.assertEqual(
                evidence["evaluation_status"],
                "passed",
                evidence,
            )
            self.assertTrue(evidence["promotion_eligible"])
            self.assertTrue(evidence["evaluator_started"])
            self.assertFalse(marker.exists())
            self.assertEqual(len(evidence["terminal_invocations"]), 1)
            self.assertEqual(
                evidence["provider_sandbox_policy_sha256"],
                fixture["descriptor"]["policy_sha256"],
            )
            self.assertEqual(
                evidence["evaluator_artifact"],
                evaluator_reference["path"],
            )
            self.assertRegex(evidence["environment_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn(
                "AGENTTEAM_CREDENTIAL_FILE",
                "\0".join(evidence["argv"]),
            )
            self.assertNotIn(
                str(fixture["credential"]),
                "\0".join(evidence["argv"]),
            )
            self.assertEqual(
                evidence["run_id"],
                "RUN-EXPERIMENT-FIXTURE",
            )
            self.assertEqual(evidence["taskpack_ids"], ["phase2-fixture"])
            self.assertEqual(
                evidence["expected_invocation_ids"],
                ["INV-FIXTURE"],
            )
            self.assertEqual(evidence["pre_run_leak_scan"]["scan_status"], "clean")
            self.assertEqual(evidence["post_run_leak_scan"]["scan_status"], "clean")
            self.assertEqual(
                validate_evaluation_evidence(
                    evidence,
                    expected_run_id="RUN-EXPERIMENT-FIXTURE",
                    expected_taskpack_ids=["phase2-fixture"],
                    expected_protocol_sha256=evidence[
                        "experiment_protocol_sha256"
                    ],
                    expected_protocol_reference_sha256=(
                        protocol_reference["sha256"]
                    ),
                    expected_acceptance_command_sha256=evidence[
                        "acceptance_command_sha256"
                    ],
                    expected_acceptance_executable_sha256=evidence[
                        "acceptance_executable_sha256"
                    ],
                    expected_evaluator_sha256=evaluator_digest,
                    expected_invocation_set_reference_sha256=(
                        invocation_set_reference["sha256"]
                    ),
                    expected_provider_sandbox_reference_sha256=(
                        sandbox_reference["sha256"]
                    ),
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    experiment_protocol_reference=protocol_reference,
                    provider_sandbox_reference=sandbox_reference,
                    canary_path=fixture["canary"],
                ),
                evidence,
            )
            self.assertTrue(evidence_path.is_file())
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "run_id binding mismatch",
            ):
                validate_evaluation_evidence(
                    evidence,
                    expected_run_id="RUN-OTHER",
                )
            forged_evidence = copy.deepcopy(evidence)
            forged_evidence["expected_invocation_ids"] = ["INV-FORGED"]
            forged_evidence["invocation_sets"][0][
                "expected_invocation_ids"
            ] = ["INV-FORGED"]
            forged_evidence["terminal_invocations"][0]["invocation_id"] = (
                "INV-FORGED"
            )
            forged_evidence["invocation_set_seal_sha256"] = hashlib.sha256(
                json.dumps(
                    forged_evidence["invocation_sets"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "invocation authority binding mismatch",
            ):
                validate_evaluation_evidence(
                    forged_evidence,
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                )
            forged_candidate = copy.deepcopy(evidence)
            forged_candidate["provider_sandbox_reference_sha256"] = "f" * 64
            forged_candidate["pre_run_leak_scan"]["canary_sha256"] = "e" * 64
            forged_candidate["post_run_leak_scan"]["canary_sha256"] = "e" * 64
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "candidate sandbox binding mismatch",
            ):
                validate_evaluation_evidence(
                    forged_candidate,
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    experiment_protocol_reference=protocol_reference,
                    provider_sandbox_reference=sandbox_reference,
                    canary_path=fixture["canary"],
                )

            candidate_probe_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                (
                    "from pathlib import Path\n"
                    "p=Path('../evaluator-only/gold-canary')\n"
                    "try:\n"
                    " p.read_bytes()\n"
                    "except OSError:\n"
                    " raise SystemExit(0)\n"
                    "raise SystemExit(91)\n"
                ),
            ]
            candidate_probe_protocol = copy.deepcopy(protocol)
            candidate_probe_protocol["acceptance"]["command"] = (
                candidate_probe_command
            )
            candidate_probe_reference = publish_experiment_protocol_reference(
                root,
                candidate_probe_protocol,
                reference_id="candidate-canary-probe",
            )
            use_real_candidate_boundary = (
                os.environ.get("AGENTTEAM_REQUIRE_REAL_BWRAP") == "1"
                and os.environ.get("AGENTTEAM_REQUIRE_SYSTEMD_EVALUATOR")
                == "1"
            )
            if use_real_candidate_boundary:
                runner_patch.stop()
            try:
                candidate_probe_evidence = run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=candidate_probe_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=candidate_probe_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            finally:
                if use_real_candidate_boundary:
                    runner_patch.start()
            self.assertEqual(
                candidate_probe_evidence["evaluation_status"],
                "passed",
                candidate_probe_evidence,
            )

            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "preregistered acceptance command",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=[
                        str(Path(sys.executable).resolve()),
                        "-c",
                        "print('different evaluator')",
                    ],
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            different = root / "different-evaluator.py"
            different.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            different.chmod(0o700)
            different_reference = publish_evaluator_reference(
                root,
                different,
                reference_id="different-evaluator",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "trusted evaluator digest mismatch",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=command,
                    cwd=fixture["repository"],
                    evaluator_reference=different_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            shell_protocol = copy.deepcopy(protocol)
            shell_command = ["/bin/bash", "-c", "true"]
            shell_protocol["acceptance"]["command"] = shell_command
            shell_protocol_reference = publish_experiment_protocol_reference(
                root,
                shell_protocol,
                reference_id="shell-protocol",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "executable is not (approved|uniquely mapped)",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=shell_protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=shell_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            ignoring_evaluator = root / "ignoring-evaluator.py"
            ignoring_evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            ignoring_evaluator.chmod(0o700)
            ignoring_reference = publish_evaluator_reference(
                root,
                ignoring_evaluator,
                reference_id="ignoring-evaluator",
            )
            failing_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "raise SystemExit(97)",
            ]
            failing_protocol = _sandbox_protocol(fixture)
            failing_protocol["acceptance"] = {
                "command": failing_command,
                "timeout_seconds": 10,
            }
            failing_protocol["evaluator"]["artifact_sha256"] = (
                ignoring_reference["sha256"]
            )
            failing_protocol_reference = (
                publish_experiment_protocol_reference(
                    root,
                    failing_protocol,
                    reference_id="ignored-acceptance-protocol",
                )
            )
            ignored_acceptance = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=failing_protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=failing_command,
                cwd=fixture["repository"],
                evaluator_reference=ignoring_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
            )
            self.assertEqual(ignored_acceptance["evaluation_status"], "failed")
            self.assertEqual(ignored_acceptance["returncode"], 97)
            self.assertFalse(ignored_acceptance["promotion_eligible"])

            fake_python = root / "python3"
            fake_python.write_text(
                "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            fake_command = [str(fake_python), "-c", "print('unsafe')"]
            fake_protocol = copy.deepcopy(protocol)
            fake_protocol["acceptance"]["command"] = fake_command
            fake_protocol_reference = publish_experiment_protocol_reference(
                root,
                fake_protocol,
                reference_id="fake-python-protocol",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "executable is not (approved|uniquely mapped)",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=fake_protocol_reference,
                    scan_scope_reference=scan_scope_reference,
                    command=fake_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            truncated_command = [
                str(Path(sys.executable).resolve()),
                "-c",
                "import sys; sys.stdout.write('A' * 64)",
            ]
            truncated_protocol = _sandbox_protocol(fixture)
            truncated_protocol["acceptance"] = {
                "command": truncated_command,
                "timeout_seconds": 10,
            }
            truncated_protocol["evaluator"]["artifact_sha256"] = (
                evaluator_digest
            )
            truncated_protocol_reference = (
                publish_experiment_protocol_reference(
                    root,
                    truncated_protocol,
                    reference_id="truncated-protocol",
                )
            )
            truncated = run_trusted_argv_evaluator(
                authority_root=root,
                invocation_set_reference=invocation_set_reference,
                provider_sandbox_reference=sandbox_reference,
                experiment_protocol_reference=truncated_protocol_reference,
                scan_scope_reference=scan_scope_reference,
                command=truncated_command,
                cwd=fixture["repository"],
                evaluator_reference=evaluator_reference,
                canary_path=fixture["canary"],
                timeout_seconds=10,
                max_output_bytes=8,
            )
            self.assertEqual(truncated["evaluation_status"], "failed")
            self.assertFalse(truncated["promotion_eligible"])
            self.assertTrue(truncated["stdout_truncated"])
            self.assertEqual(
                truncated["failure_reason"],
                "evaluator_output_truncated",
            )

            valid_terminal = lifecycle.terminal_path.read_text(encoding="utf-8")
            terminal = json.loads(valid_terminal)
            terminal["lifecycle_owner_token"] = "OWNER-TAMPERED"
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "does not bind its start and sandbox",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            lifecycle.terminal_path.write_text(valid_terminal, encoding="utf-8")

            terminal = json.loads(valid_terminal)
            terminal.pop("experiment_sandbox_policy_sha256", None)
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "does not bind its start and sandbox",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            lifecycle.terminal_path.write_text(valid_terminal, encoding="utf-8")

            terminal = json.loads(
                lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            terminal["terminal_status"] = "running"
            lifecycle.terminal_path.write_text(
                json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "model invocation terminal schema failed",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=(
                        truncated_protocol_reference
                    ),
                    scan_scope_reference=scan_scope_reference,
                    command=truncated_command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

    def test_precreated_invocation_cannot_publish_after_evaluation_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _sandbox_fixture(root)
            sandbox_reference = _publish_test_sandbox_reference(root, fixture)
            lifecycle_authority_root = (
                experiment_lifecycle_authority_root(
                    root,
                    "completed",
                )
            )
            context = _model_context(supported=False, sandbox_reference=None)
            context["experiment_sandbox_policy_sha256"] = fixture[
                "descriptor"
            ]["policy_sha256"]
            context["experiment_sandbox_reference_sha256"] = (
                sandbox_reference["sha256"]
            )
            completed = InvocationLifecycle(
                lifecycle_authority_root,
                context,
                invocation_id="INV-COMPLETED",
            )
            completed.publish_start(ExecutionGroupIdentity.not_applicable())
            completed.finalize("completed", stdout="", stderr="")
            late = InvocationLifecycle(
                lifecycle_authority_root,
                context,
                invocation_id="INV-LATE",
            )
            scan_paths = {}
            for group in ("prompt", "context", "taskpack", "artifacts"):
                path = root / f"{group}.txt"
                path.write_text("{}\n", encoding="utf-8")
                scan_paths[group] = [path]
            scan_reference = publish_scan_scope_reference(root, scan_paths)
            evaluator = root / "evaluator.py"
            evaluator.write_text(
                "#!/usr/bin/python3\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            evaluator.chmod(0o700)
            evaluator_reference = publish_evaluator_reference(root, evaluator)
            shadow_authority = lifecycle_authority_root.parent / "shadow"
            shadow_authority.mkdir()
            shutil.copytree(
                completed.invocation_dir,
                shadow_authority / "model_invocations" / "INV-COMPLETED",
                dirs_exist_ok=True,
            )
            with self.assertRaisesRegex(
                ExperimentSandboxError,
                "does not cover the lifecycle registry",
            ):
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": [
                                "INV-COMPLETED",
                                "INV-LATE",
                            ],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                    reference_id="shadow-manifest",
                )
            shutil.rmtree(shadow_authority)
            hidden_authority = root / "temporarily-hidden-authority"
            lifecycle_authority_root.rename(hidden_authority)
            try:
                with self.assertRaisesRegex(
                    ExperimentSandboxError,
                    "lifecycle authority.*unavailable",
                ):
                    publish_model_invocation_set_reference(
                        root,
                        "RUN-EXPERIMENT-FIXTURE",
                        [
                            {
                                "lifecycle_authority_root": (
                                    lifecycle_authority_root
                                ),
                                "taskpack_id": "phase2-fixture",
                                "invocation_ids": [
                                    "INV-COMPLETED",
                                    "INV-LATE",
                                ],
                                "sandbox_reference": sandbox_reference,
                            }
                        ],
                        reference_id="deleted-root-manifest",
                    )
            finally:
                hidden_authority.rename(lifecycle_authority_root)
            invocation_set_reference = (
                publish_model_invocation_set_reference(
                    root,
                    "RUN-EXPERIMENT-FIXTURE",
                    [
                        {
                            "lifecycle_authority_root": (
                                lifecycle_authority_root
                            ),
                            "taskpack_id": "phase2-fixture",
                            "invocation_ids": [
                                "INV-COMPLETED",
                                "INV-LATE",
                            ],
                            "sandbox_reference": sandbox_reference,
                        }
                    ],
                )
            )
            protocol = _sandbox_protocol(fixture)
            protocol["acceptance"]["command"] = [
                str(Path(sys.executable).resolve()),
                "-c",
                "raise SystemExit(0)",
            ]
            protocol["acceptance"]["timeout_seconds"] = 10
            protocol["evaluator"]["artifact_sha256"] = evaluator_reference[
                "sha256"
            ]
            protocol_reference = publish_experiment_protocol_reference(
                root,
                protocol,
            )

            with self.assertRaisesRegex(
                ExperimentEvaluationBlocked,
                "durable start",
            ):
                run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_set_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_reference,
                    command=protocol["acceptance"]["command"],
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )
            with self.assertRaisesRegex(
                ModelInvocationIntegrityError,
                "invocation set is sealed",
            ):
                late.publish_start(ExecutionGroupIdentity.not_applicable())

    def test_evaluation_seals_multiple_taskpack_authority_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "agentteam_runtime.experiment_sandbox._run_bounded_argv",
                side_effect=_test_evaluator_execution,
            ):
                root = Path(tmp)
                fixture = _sandbox_fixture(root)
                worker_sandbox_reference = (
                    _publish_test_sandbox_reference(
                        root,
                        fixture,
                    )
                )
                author_repository = root / "author-repository"
                shutil.copytree(
                    fixture["repository"],
                    author_repository,
                )
                author_identity = {
                    "commit": _git(
                        author_repository,
                        "rev-parse",
                        "HEAD",
                    ).stdout.strip(),
                    "tree": _git(
                        author_repository,
                        "rev-parse",
                        "HEAD^{tree}",
                    ).stdout.strip(),
                    "git_object_format": _git(
                        author_repository,
                        "rev-parse",
                        "--show-object-format",
                    ).stdout.strip(),
                }
                author_descriptor = build_provider_sandbox_descriptor(
                    author_repository,
                    runtime_views=[
                        {
                            "source": view["source"],
                            "target": view["target"],
                        }
                        for view in fixture["descriptor"]["runtime_views"]
                    ],
                    bwrap_path="/usr/bin/bwrap",
                    repository_target="/workspace-author",
                    repository_identity=author_identity,
                    forbidden_paths=[fixture["canary"]],
                )
                author_evidence = dict(fixture["evidence"])
                author_evidence["policy_sha256"] = author_descriptor[
                    "policy_sha256"
                ]
                with patch(
                    "agentteam_runtime.experiment_sandbox."
                    "probe_gold_canary_denial",
                    return_value=author_evidence,
                ):
                    author_sandbox_reference = (
                        publish_provider_sandbox_reference(
                            root,
                            author_descriptor,
                            fixture["canary"],
                            reference_id="author-sandbox",
                        )
                    )
                author_authority = experiment_lifecycle_authority_root(
                    root,
                    "author-context",
                )
                cross_context = _model_context(
                    supported=False,
                    sandbox_reference=author_sandbox_reference,
                )
                cross_context["experiment_sandbox_required"] = True
                cross_context["experiment_authority_root"] = str(root)
                cross_invocation = ModelInvocationCall(
                    author_authority,
                    cross_context,
                    supported=False,
                )
                with self.assertRaisesRegex(
                    ModelInvocationIntegrityError,
                    "cwd must remain inside",
                ):
                    cross_invocation.execute(
                        [str(Path(sys.executable).resolve()), "-c", "pass"],
                        cwd=fixture["repository"],
                        input_text="",
                        timeout_seconds=10,
                    )
                shutil.rmtree(cross_invocation.lifecycle.invocation_dir)
                worker_authority = experiment_lifecycle_authority_root(
                    root,
                    "worker-output",
                )
                authorities = {
                    "author-context": author_authority,
                    "worker-output": worker_authority,
                }
                invocation_sets = []
                for (
                    authority_name,
                    taskpack_id,
                    sandbox_reference,
                    sandbox_policy_sha256,
                    workspace,
                ) in (
                    (
                        "author-context",
                        "taskpack-authoring",
                        author_sandbox_reference,
                        author_descriptor["policy_sha256"],
                        author_repository,
                    ),
                    (
                        "worker-output",
                        "taskpack-implementation",
                        worker_sandbox_reference,
                        fixture["descriptor"]["policy_sha256"],
                        fixture["repository"],
                    ),
                ):
                    authority = authorities[authority_name]
                    context = _model_context(
                        supported=False,
                        sandbox_reference=sandbox_reference,
                    )
                    context["taskpack_id"] = taskpack_id
                    context["experiment_sandbox_required"] = True
                    context["experiment_authority_root"] = str(root)
                    invocation = ModelInvocationCall(
                        authority,
                        context,
                        supported=False,
                    )
                    with patch(
                        "agentteam_runtime.model_invocation."
                        "_run_bounded_process",
                        return_value=ProviderExecution([], 0, "", ""),
                    ):
                        invocation.execute(
                            [
                                str(Path(sys.executable).resolve()),
                                "-c",
                                "raise SystemExit(0)",
                            ],
                            cwd=workspace,
                            input_text="",
                            timeout_seconds=10,
                        )
                    invocation.lifecycle.finalize(
                        "completed",
                        stdout="",
                        stderr="",
                    )
                    self.assertEqual(
                        invocation.lifecycle.context[
                            "experiment_sandbox_policy_sha256"
                        ],
                        sandbox_policy_sha256,
                    )
                    invocation_id = invocation.lifecycle.invocation_id
                    invocation_sets.append(
                        {
                            "lifecycle_authority_root": authority,
                            "taskpack_id": taskpack_id,
                            "invocation_ids": [invocation_id],
                            "sandbox_reference": sandbox_reference,
                        }
                    )
                sandbox_reference = worker_sandbox_reference
                scan_groups = {}
                for group in ("prompt", "context", "taskpack", "artifacts"):
                    path = root / f"{group}.json"
                    path.write_text("{}\n", encoding="utf-8")
                    scan_groups[group] = [path]
                scan_reference = publish_scan_scope_reference(
                    root,
                    scan_groups,
                )
                invocation_reference = (
                    publish_model_invocation_set_reference(
                        root,
                        "RUN-EXPERIMENT-FIXTURE",
                        invocation_sets,
                    )
                )
                evaluator = root / "multi-root-evaluator.py"
                evaluator.write_text(
                    "#!/usr/bin/python3\n"
                    "import sys\n"
                    "if len(sys.argv) < 3 or sys.argv[1] != '--':\n"
                    "    raise SystemExit(64)\n",
                    encoding="utf-8",
                )
                evaluator.chmod(0o700)
                evaluator_reference = publish_evaluator_reference(
                    root,
                    evaluator,
                )
                protocol = _sandbox_protocol(fixture)
                command = [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    "raise SystemExit(0)",
                ]
                protocol["acceptance"] = {
                    "command": command,
                    "timeout_seconds": 10,
                }
                protocol["evaluator"]["artifact_sha256"] = (
                    evaluator_reference["sha256"]
                )
                protocol_reference = publish_experiment_protocol_reference(
                    root,
                    protocol,
                )

                evidence = run_trusted_argv_evaluator(
                    authority_root=root,
                    invocation_set_reference=invocation_reference,
                    provider_sandbox_reference=sandbox_reference,
                    experiment_protocol_reference=protocol_reference,
                    scan_scope_reference=scan_reference,
                    command=command,
                    cwd=fixture["repository"],
                    evaluator_reference=evaluator_reference,
                    canary_path=fixture["canary"],
                    timeout_seconds=10,
                )

            self.assertEqual(
                evidence["evaluation_status"],
                "passed",
                evidence,
            )
            self.assertEqual(
                evidence["taskpack_ids"],
                ["taskpack-authoring", "taskpack-implementation"],
            )
            self.assertEqual(
                evidence["expected_invocation_ids"],
                sorted(
                    invocation_id
                    for item in invocation_sets
                    for invocation_id in item["invocation_ids"]
                ),
            )
            self.assertEqual(len(evidence["invocation_sets"]), 2)
            self.assertEqual(len(evidence["terminal_invocations"]), 2)
            self.assertEqual(
                {
                    item["sandbox_policy_sha256"]
                    for item in evidence["invocation_sets"]
                },
                {
                    author_descriptor["policy_sha256"],
                    fixture["descriptor"]["policy_sha256"],
                },
            )

    def test_prompt_context_taskpack_and_artifact_canary_scans_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _sandbox_fixture(tmp)
            root = Path(tmp)
            group_paths = {}
            for group in ("prompt", "context", "taskpack", "artifacts"):
                path = root / f"{group}.txt"
                path.write_text(f"safe {group}\n", encoding="utf-8")
                group_paths[group] = [path]
            clean = scan_canary_leakage(
                group_paths,
                canary_path=fixture["canary"],
            )
            self.assertEqual(clean["scan_status"], "clean")

            digest = hashlib.sha256(fixture["canary"].read_bytes()).hexdigest()
            Path(group_paths["context"][0]).write_text(
                f"context leaked {digest}\n",
                encoding="utf-8",
            )
            leaked = scan_canary_leakage(
                group_paths,
                canary_path=fixture["canary"],
            )
            self.assertEqual(leaked["scan_status"], "leak_detected")
            self.assertEqual(leaked["findings"][0]["group"], "context")
            self.assertEqual(leaked["findings"][0]["match"], "canary_sha256")

            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "byte bound",
            ):
                scan_canary_leakage(
                    group_paths,
                    canary_path=fixture["canary"],
                    max_bytes=1,
                )
            with self.assertRaisesRegex(
                ExperimentSandboxUnavailable,
                "entry bound",
            ):
                scan_canary_leakage(
                    group_paths,
                    canary_path=fixture["canary"],
                    max_files=3,
                )


if __name__ == "__main__":
    unittest.main()

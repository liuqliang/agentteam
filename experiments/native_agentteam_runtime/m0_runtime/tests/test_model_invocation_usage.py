import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from agentteam_runtime.token_usage import (
    TOKEN_USAGE_FIELDS,
    aggregate_token_usage,
    parse_terminal_usage_from_jsonl,
    token_usage_from_jsonl,
    usage_event_id_for_invocation,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"


def _schema(name):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _validator(name, *, resolver=None):
    return Draft202012Validator(
        _schema(name),
        format_checker=FormatChecker(),
        resolver=resolver,
    )


def _supported_start():
    return {
        "start_schema_version": "model_invocation_started.v1",
        "invocation_id": "INV-example-001",
        "project": "agentteam",
        "run_id": "phase1-example",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase1-example",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "TASK-001",
        "attempt_id": "ATTEMPT-001",
        "runtime_execution_session_id": "runtime-session-001",
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": "LEASE-001",
        "agent_id": "agent-implementation-worker-1",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": None,
        "coverage_class": "supported_model_invocation",
        "gated_supervisor_pid": 1234,
        "gated_supervisor_pgid": 1234,
        "host_boot_id": "12345678-1234-1234-1234-123456789abc",
        "gated_supervisor_start_ticks": 998877,
        "launch_nonce_sha256": "a" * 64,
        "systemd_linger_enabled": True,
        "systemd_transient_unit": "agentteam-inv-example-001.service",
        "systemd_transient_invocation_id": "b" * 32,
        "systemd_transient_kill_mode": "control-group",
        "systemd_user_manager_identity": "user-manager-1000-boot-1",
        "systemd_transient_control_group": (
            "/user.slice/user-1000.slice/user@1000.service/"
            "app.slice/agentteam-inv-example-001.service"
        ),
        "systemd_user_service_invocation_id": "c" * 32,
        "systemd_user_service_control_group": (
            "/user.slice/user-1000.slice/user@1000.service"
        ),
        "systemd_user_service_kill_mode": "control-group",
        "started_at": "2026-07-23T00:00:00Z",
    }


def _reported_usage():
    return {
        "usage_schema_version": "model_invocation_usage.v1",
        "usage_event_id": "USAGE-example-001",
        "invocation_id": "INV-example-001",
        "project": "agentteam",
        "run_id": "phase1-example",
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": "phase1-example",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": "TASK-001",
        "attempt_id": "ATTEMPT-001",
        "runtime_execution_session_id": "runtime-session-001",
        "provider_session_id": None,
        "provider_predecessor_invocation_id": None,
        "provider_turn_id": None,
        "provider_predecessor_turn_id": None,
        "lifecycle_owner_token": "LEASE-001",
        "terminal_writer": "worker",
        "agent_id": "agent-implementation-worker-1",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "backend": "codex",
        "model": None,
        "coverage_class": "supported_model_invocation",
        "terminal_status": "completed",
        "usage_status": "reported",
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": "provider_reported",
        "provider_usage_snapshot": None,
        "unavailable_reason": None,
        "input_tokens": 1200,
        "cached_input_tokens": 800,
        "output_tokens": 260,
        "reasoning_tokens": None,
        "total_tokens": 1460,
        "started_at": "2026-07-23T00:00:00Z",
        "finished_at": "2026-07-23T00:01:00Z",
        "wall_time_seconds": 60.0,
        "source_artifact_path": "runs/phase1-example/events.jsonl",
    }


class ModelInvocationSchemaTests(unittest.TestCase):
    def test_all_new_schemas_are_valid_draft_2020_12(self):
        for name in (
            "model_invocation_started.schema.json",
            "model_invocation_writer_revoked.schema.json",
            "model_invocation_usage.schema.json",
            "model_invocation_live_smoke.schema.json",
        ):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(_schema(name))

    def test_supported_start_requires_exact_execution_group_identity(self):
        validator = _validator("model_invocation_started.schema.json")
        start = _supported_start()

        validator.validate(start)
        self.assertFalse(any(key.endswith("_tokens") for key in start))

        for field in (
            "systemd_transient_unit",
            "systemd_transient_invocation_id",
            "systemd_user_manager_identity",
            "systemd_transient_control_group",
            "systemd_user_service_invocation_id",
            "systemd_user_service_control_group",
            "systemd_user_service_kill_mode",
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(start)
                invalid[field] = None
                self.assertTrue(list(validator.iter_errors(invalid)))

    def test_not_applicable_start_does_not_invent_execution_group_identity(self):
        validator = _validator("model_invocation_started.schema.json")
        start = _supported_start()
        start["coverage_class"] = "not_applicable_adapter"
        start["backend"] = "fake"
        for field in (
            "gated_supervisor_pid",
            "gated_supervisor_pgid",
            "host_boot_id",
            "gated_supervisor_start_ticks",
            "launch_nonce_sha256",
            "systemd_linger_enabled",
            "systemd_transient_unit",
            "systemd_transient_invocation_id",
            "systemd_transient_kill_mode",
            "systemd_user_manager_identity",
            "systemd_transient_control_group",
            "systemd_user_service_invocation_id",
            "systemd_user_service_control_group",
            "systemd_user_service_kill_mode",
        ):
            start[field] = None

        validator.validate(start)
        start["systemd_transient_unit"] = "invented.service"
        self.assertTrue(list(validator.iter_errors(start)))

    def test_start_and_terminal_join_and_enforce_utc_and_token_constraints(self):
        start_validator = _validator("model_invocation_started.schema.json")
        usage_validator = _validator("model_invocation_usage.schema.json")
        start = _supported_start()
        usage = _reported_usage()

        start_validator.validate(start)
        usage_validator.validate(usage)
        self.assertEqual(start["invocation_id"], usage["invocation_id"])
        self.assertEqual(start["coverage_class"], usage["coverage_class"])

        invalid = copy.deepcopy(usage)
        invalid["input_tokens"] = -1
        self.assertTrue(list(usage_validator.iter_errors(invalid)))
        invalid = copy.deepcopy(usage)
        invalid["finished_at"] = "2026-07-23T08:01:00+08:00"
        self.assertTrue(list(usage_validator.iter_errors(invalid)))

    def test_partial_and_unavailable_usage_require_a_reason(self):
        validator = _validator("model_invocation_usage.schema.json")
        for status in ("partial", "unavailable"):
            with self.subTest(status=status):
                usage = _reported_usage()
                usage["usage_status"] = status
                usage["accounting_method"] = "unavailable"
                usage["unavailable_reason"] = None
                for field in TOKEN_USAGE_FIELDS:
                    usage[field] = None
                self.assertTrue(list(validator.iter_errors(usage)))
                usage["unavailable_reason"] = "provider_session_lineage_ambiguous"
                validator.validate(usage)

    def test_not_applicable_coverage_cannot_be_reclassified_as_reported(self):
        validator = _validator("model_invocation_usage.schema.json")
        usage = _reported_usage()
        usage["coverage_class"] = "not_applicable_adapter"

        self.assertTrue(list(validator.iter_errors(usage)))

    def test_acceptance_records_require_logical_run_and_gate_epoch(self):
        start_validator = _validator("model_invocation_started.schema.json")
        usage_validator = _validator("model_invocation_usage.schema.json")
        start = _supported_start()
        usage = _reported_usage()
        for record in (start, usage):
            record["usage_stage"] = "acceptance_live_smoke"
            record["run_id"] = "phase1-example-acceptance-attempt-1"
            record["implementation_run_id"] = "phase1-example"
            record["gate_epoch"] = 1

        start_validator.validate(start)
        usage_validator.validate(usage)
        start["gate_epoch"] = None
        self.assertTrue(list(start_validator.iter_errors(start)))

    def test_revocation_binds_one_invocation_and_exact_nonempty_owner(self):
        validator = _validator("model_invocation_writer_revoked.schema.json")
        revocation = {
            "revocation_schema_version": "model_invocation_writer_revoked.v1",
            "invocation_id": "INV-example-001",
            "lifecycle_owner_token": "LEASE-001",
            "revoked_by": "recovery-controller-1",
            "revoked_at": "2026-07-23T00:02:00Z",
            "reason": "worker_process_confirmed_dead",
        }
        validator.validate(revocation)
        revocation["lifecycle_owner_token"] = ""
        self.assertTrue(list(validator.iter_errors(revocation)))

    def test_live_smoke_uses_declared_git_object_format(self):
        start_schema = _schema("model_invocation_started.schema.json")
        usage_schema = _schema("model_invocation_usage.schema.json")
        live_schema = _schema("model_invocation_live_smoke.schema.json")
        resolver = RefResolver.from_schema(
            live_schema,
            store={
                start_schema["$id"]: start_schema,
                usage_schema["$id"]: usage_schema,
            },
        )
        validator = _validator(
            "model_invocation_live_smoke.schema.json",
            resolver=resolver,
        )
        start = _supported_start()
        usage = _reported_usage()
        for record in (start, usage):
            record["usage_stage"] = "acceptance_live_smoke"
            record["run_id"] = "phase1-example-acceptance-attempt-1"
            record["implementation_run_id"] = "phase1-example"
            record["gate_epoch"] = 3
        live = {
            "schema_version": "model_invocation_live_smoke.v1",
            "project": "agentteam",
            "run_id": "phase1-example-acceptance-attempt-1",
            "run_kind": "acceptance_evidence",
            "taskpack_id": "phase1-example",
            "implementation_run_id": "phase1-example",
            "gate_epoch": 3,
            "acceptance_series_id": "P1-LIVE",
            "acceptance_attempt_id": "attempt-1",
            "git_object_format": "sha1",
            "candidate_commit_sha": "1" * 40,
            "validated_code_sha": "2" * 40,
            "candidate_runtime_root": "/tmp/candidate/m0_runtime",
            "terminal_status": "completed",
            "invocation_start_record": start,
            "invocation_usage_record": usage,
            "provider_terminal_snapshot_sha256": "3" * 64,
            "provider_totals": {
                "input_tokens": 1200,
                "cached_input_tokens": 800,
                "output_tokens": 260,
                "reasoning_tokens": None,
                "total_tokens": 1460,
            },
            "reconciliation_status": "matched",
            "controller_validation_status": "passed",
            "started_at": "2026-07-23T00:00:00Z",
            "finished_at": "2026-07-23T00:01:00Z",
            "bounded_raw_spool_path": (
                "runs/phase1-example-acceptance-attempt-1/"
                "acceptance/runtime-output/provider.jsonl"
            ),
        }

        validator.validate(live)
        live["candidate_commit_sha"] = "1" * 64
        self.assertTrue(list(validator.iter_errors(live)))

    def test_canonical_event_enum_predeclares_invocation_lifecycle(self):
        event_types = set(_schema("event.schema.json")["properties"]["event_type"]["enum"])
        self.assertTrue(
            {
                "model_invocation_started",
                "model_invocation_writer_revoked",
                "model_invocation_usage_recorded",
            }.issubset(event_types)
        )


class TerminalUsageParserTests(unittest.TestCase):
    @staticmethod
    def _event(usage, **metadata):
        return json.dumps(
            {
                "type": "turn.completed",
                "usage": usage,
                **metadata,
            },
            sort_keys=True,
        )

    def test_usage_event_id_is_stable_and_invocation_scoped(self):
        first = usage_event_id_for_invocation("INV-example-001")
        replayed = usage_event_id_for_invocation("INV-example-001")
        different = usage_event_id_for_invocation("INV-example-002")

        self.assertEqual(first, replayed)
        self.assertNotEqual(first, different)
        self.assertRegex(first, r"^USAGE-[0-9a-f]{64}$")
        with self.assertRaises(ValueError):
            usage_event_id_for_invocation("example-001")

    def test_repeated_cumulative_snapshots_select_final_total_once(self):
        text = "\n".join(
            [
                self._event(
                    {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    }
                ),
                self._event(
                    {
                        "input_tokens": 18,
                        "output_tokens": 5,
                        "total_tokens": 23,
                    }
                ),
            ]
        )

        usage = parse_terminal_usage_from_jsonl(text)

        self.assertEqual(usage["usage_status"], "reported")
        self.assertEqual(usage["input_tokens"], 18)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 23)

    def test_parser_ignores_malformed_nonterminal_and_nested_usage(self):
        text = "\n".join(
            [
                "{malformed",
                json.dumps(
                    {
                        "type": "item.completed",
                        "result": {
                            "usage": {
                                "input_tokens": 900,
                                "output_tokens": 100,
                                "total_tokens": 1000,
                            }
                        },
                    }
                ),
                self._event(
                    {
                        "input_tokens": 9,
                        "output_tokens": 1,
                        "total_tokens": 10,
                    }
                ),
            ]
        )

        self.assertEqual(token_usage_from_jsonl(text)["total_tokens"], 10)
        self.assertIsNone(
            token_usage_from_jsonl(
                json.dumps(
                    {
                        "type": "item.completed",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                )
            )
        )

    def test_partial_terminal_usage_is_not_guessed(self):
        usage = parse_terminal_usage_from_jsonl(
            self._event({"input_tokens": 7, "output_tokens": 3})
        )

        self.assertEqual(usage["usage_status"], "partial")
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(
            usage["unavailable_reason"],
            "incomplete_provider_usage",
        )

    def test_cached_and_reasoning_fields_do_not_inflate_total(self):
        usage = parse_terminal_usage_from_jsonl(
            self._event(
                {
                    "input_tokens": 10,
                    "cached_input_tokens": 8,
                    "output_tokens": 5,
                    "reasoning_tokens": 3,
                    "total_tokens": 15,
                }
            )
        )

        self.assertEqual(usage["total_tokens"], 15)
        self.assertNotEqual(
            usage["total_tokens"],
            usage["input_tokens"]
            + usage["cached_input_tokens"]
            + usage["output_tokens"]
            + usage["reasoning_tokens"],
        )

    def test_authoritative_session_lineage_produces_one_monotonic_delta(self):
        text = self._event(
            {
                "input_tokens": 130,
                "cached_input_tokens": 40,
                "output_tokens": 45,
                "reasoning_tokens": 12,
                "total_tokens": 175,
            },
            provider_usage_scope="session_cumulative",
            provider_session_id="provider-session-1",
            provider_turn_id="turn-2",
            provider_predecessor_turn_id="turn-1",
        )
        previous = {
            "provider_session_id": "provider-session-1",
            "invocation_id": "INV-prior",
            "provider_turn_id": "turn-1",
            "input_tokens": 100,
            "cached_input_tokens": 30,
            "output_tokens": 30,
            "reasoning_tokens": 8,
            "total_tokens": 130,
        }

        usage = parse_terminal_usage_from_jsonl(
            text,
            previous_provider_snapshot=previous,
            provider_predecessor_invocation_id="INV-prior",
            session_writer_exclusive=True,
            project_binding_matches=True,
        )

        self.assertEqual(usage["usage_status"], "reported")
        self.assertEqual(usage["accounting_method"], "session_delta")
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 15)
        self.assertEqual(usage["reasoning_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 45)
        self.assertEqual(usage["provider_usage_snapshot"]["total_tokens"], 175)

    def test_ambiguous_session_snapshot_has_no_invocation_lower_bound(self):
        text = self._event(
            {
                "input_tokens": 130,
                "cached_input_tokens": 40,
                "output_tokens": 45,
                "reasoning_tokens": 12,
                "total_tokens": 175,
            },
            provider_usage_scope="session_cumulative",
            provider_session_id="provider-session-1",
            provider_turn_id="turn-2",
        )

        usage = parse_terminal_usage_from_jsonl(text)
        aggregate = aggregate_token_usage([usage], expected_count=1)

        self.assertEqual(usage["usage_status"], "unavailable")
        self.assertEqual(
            usage["unavailable_reason"],
            "provider_session_lineage_ambiguous",
        )
        self.assertTrue(all(usage[field] is None for field in TOKEN_USAGE_FIELDS))
        self.assertTrue(
            all(aggregate[field] is None for field in TOKEN_USAGE_FIELDS)
        )

    def test_resume_last_concurrent_forked_and_non_monotonic_are_rejected(self):
        base_event = self._event(
            {
                "input_tokens": 90,
                "output_tokens": 30,
                "total_tokens": 120,
            },
            provider_usage_scope="session_cumulative",
            provider_session_id="provider-session-1",
            provider_turn_id="turn-2",
            provider_predecessor_turn_id="turn-1",
        )
        previous = {
            "provider_session_id": "provider-session-1",
            "invocation_id": "INV-prior",
            "provider_turn_id": "turn-1",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        }
        common = {
            "previous_provider_snapshot": previous,
            "provider_predecessor_invocation_id": "INV-prior",
            "session_writer_exclusive": True,
            "project_binding_matches": True,
        }
        cases = (
            {"resume_mode": "resume_last"},
            {"concurrent": True},
            {"forked": True},
            {},
        )
        for context in cases:
            with self.subTest(context=context):
                usage = parse_terminal_usage_from_jsonl(
                    base_event,
                    **common,
                    **context,
                )
                self.assertEqual(usage["usage_status"], "unavailable")
                self.assertTrue(
                    all(usage[field] is None for field in TOKEN_USAGE_FIELDS)
                )

    def test_replay_is_byte_equivalent(self):
        text = self._event(
            {
                "input_tokens": 20,
                "cached_input_tokens": 10,
                "output_tokens": 4,
                "reasoning_tokens": None,
                "total_tokens": 24,
            }
        )
        first = parse_terminal_usage_from_jsonl(text)
        second = parse_terminal_usage_from_jsonl(text)

        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )


if __name__ == "__main__":
    unittest.main()

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.decision_ledger import (
    DecisionLedger,
    DecisionLedgerError,
    DecisionLedgerIntegrityError,
    decision_record_sha256,
    load_decision_ledger,
    validate_decision_artifact_link,
    validate_decision_record,
)
from agentteam_runtime.projection_db import (
    check_project_projection_db,
    project_projection_db_path,
    read_projected_decision_graph,
    read_projected_decisions,
    rebuild_project_projection_db,
)


class DecisionLedgerTests(unittest.TestCase):
    def _decision(
        self,
        decision_id="DEC-root",
        *,
        revision=1,
        kind="direction",
        subject="project_direction",
        level="L2",
        parent=None,
        supersedes=None,
        status="active",
        previous=None,
    ):
        return {
            "schema_version": "decision_record.v1",
            "decision_id": decision_id,
            "revision": revision,
            "decision_kind": kind,
            "subject": subject,
            "authority_level": level,
            "parent_decision_id": parent,
            "supersedes_decision_id": supersedes,
            "statement": "Organize execution around an explicit decision.",
            "selected_option": "decision_centric_execution",
            "alternatives": [
                {
                    "option": "run_centric_execution",
                    "rejected_reason": "Intent must not be reconstructed from runs.",
                }
            ],
            "rationale": "Recovery needs the selected intent and its evidence.",
            "scope": ["runtime", "recovery"],
            "expected_outcome": "Execution can resume from a decision and Git state.",
            "acceptance_refs": ["decision-projection", "git-recovery"],
            "status": status,
            "created_at": "2026-08-06T01:00:00Z",
            "created_by": "operator",
            "previous_revision_sha256": previous,
        }

    def _artifact(
        self,
        artifact_id="ART-contract",
        *,
        decision_id="DEC-root",
        kind="contract",
        locator="path:plans/decision.md",
        algorithm="sha256",
        digest=None,
    ):
        digest = digest or ("a" * (40 if algorithm == "git_sha1" else 64))
        return {
            "schema_version": "decision_artifact_link.v1",
            "artifact_id": artifact_id,
            "decision_id": decision_id,
            "artifact_kind": kind,
            "locator": locator,
            "digest_algorithm": algorithm,
            "digest": digest,
            "producer": "operator",
            "created_at": "2026-08-06T01:01:00Z",
        }

    def test_schema_keeps_decision_and_artifact_vocabularies_small(self):
        validate_decision_record(self._decision())
        validate_decision_artifact_link(self._artifact())

        invalid_decision = self._decision()
        invalid_decision["decision_kind"] = "routing"
        with self.assertRaisesRegex(DecisionLedgerError, "decision_kind"):
            validate_decision_record(invalid_decision)

        invalid_level = self._decision()
        invalid_level["authority_level"] = "L0"
        with self.assertRaisesRegex(DecisionLedgerError, "authority_level"):
            validate_decision_record(invalid_level)

        invalid_artifact = self._artifact()
        invalid_artifact["artifact_kind"] = "heartbeat"
        with self.assertRaisesRegex(DecisionLedgerError, "artifact_kind"):
            validate_decision_artifact_link(invalid_artifact)

    def test_append_replay_revision_and_graph_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            root = self._decision()
            self.assertTrue(ledger.append_decision(root)["created"])
            self.assertFalse(ledger.append_decision(root)["created"])

            child = self._decision(
                "DEC-execute",
                kind="execution",
                subject="implementation_route",
                parent="DEC-root",
            )
            ledger.append_decision(child)
            contract = self._artifact()
            code = self._artifact(
                "ART-code",
                decision_id="DEC-execute",
                kind="code_state",
                locator="git:commit:" + "b" * 40,
                algorithm="git_sha1",
                digest="b" * 40,
            )
            self.assertTrue(ledger.append_artifact_link(contract)["created"])
            self.assertTrue(ledger.append_artifact_link(code)["created"])
            self.assertFalse(ledger.append_artifact_link(code)["created"])

            completed = copy.deepcopy(child)
            completed["revision"] = 2
            completed["status"] = "completed"
            completed["previous_revision_sha256"] = decision_record_sha256(child)
            completed["created_at"] = "2026-08-06T01:02:00Z"
            ledger.append_decision(completed)

            reopened = load_decision_ledger(temporary)
            latest = {
                item["decision_id"]: item for item in reopened.latest_decisions()
            }
            self.assertEqual(latest["DEC-execute"]["revision"], 2)
            self.assertEqual(latest["DEC-execute"]["status"], "completed")
            graph = reopened.decision_graph("DEC-root")
            self.assertEqual(
                [item["artifact_id"] for item in graph["artifacts"]],
                ["ART-contract"],
            )
            self.assertEqual(
                graph["children"][0]["decision"]["decision_id"],
                "DEC-execute",
            )
            self.assertEqual(
                [item["artifact_id"] for item in graph["children"][0]["artifacts"]],
                ["ART-code"],
            )
            self.assertEqual(
                sorted(path.name for path in (Path(temporary) / "decisions").iterdir()),
                ["decision-ledger.jsonl", "decision-ledger.lock"],
            )

    def test_unknown_edges_conflicting_replay_and_invalid_transitions_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            root = self._decision()
            ledger.append_decision(root)

            unknown_parent = self._decision(
                "DEC-child",
                parent="DEC-missing",
            )
            with self.assertRaisesRegex(DecisionLedgerError, "unknown decision"):
                ledger.append_decision(unknown_parent)

            dangling = self._artifact(decision_id="DEC-missing")
            with self.assertRaisesRegex(DecisionLedgerError, "unknown decision"):
                ledger.append_artifact_link(dangling)

            conflict = copy.deepcopy(root)
            conflict["statement"] = "A conflicting replay."
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "replay conflicts",
            ):
                ledger.append_decision(conflict)

            completed = copy.deepcopy(root)
            completed["revision"] = 2
            completed["status"] = "completed"
            completed["previous_revision_sha256"] = decision_record_sha256(root)
            ledger.append_decision(completed)
            invalid = copy.deepcopy(completed)
            invalid["revision"] = 3
            invalid["status"] = "active"
            invalid["previous_revision_sha256"] = decision_record_sha256(completed)
            with self.assertRaisesRegex(DecisionLedgerError, "invalid decision status"):
                ledger.append_decision(invalid)

    def test_git_and_path_locators_are_constrained_by_artifact_kind(self):
        with self.assertRaisesRegex(DecisionLedgerError, "requires code_state"):
            validate_decision_artifact_link(
                self._artifact(
                    locator="git:commit:" + "a" * 40,
                    algorithm="git_sha1",
                )
            )
        with self.assertRaisesRegex(DecisionLedgerError, "safe and relative"):
            validate_decision_artifact_link(
                self._artifact(locator="path:../secret")
            )
        with self.assertRaisesRegex(DecisionLedgerError, "digest length"):
            validate_decision_artifact_link(
                self._artifact(digest="a" * 40)
            )

    def test_hash_chain_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            ledger.append_decision(self._decision())
            path = ledger.path
            entry = json.loads(path.read_text(encoding="utf-8"))
            entry["payload"]["rationale"] = "tampered"
            path.write_text(
                json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "digest is invalid",
            ):
                load_decision_ledger(temporary)

    def test_incomplete_terminal_line_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            ledger.path.write_bytes(b'{"partial":true}')
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "incomplete terminal line",
            ):
                load_decision_ledger(temporary)

    def test_projection_rebuilds_decisions_and_falls_back_to_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            root = self._decision()
            child = self._decision(
                "DEC-execute",
                kind="execution",
                subject="implementation_route",
                parent="DEC-root",
            )
            ledger.append_decision(root)
            ledger.append_decision(child)
            ledger.append_artifact_link(self._artifact())

            rebuilt = rebuild_project_projection_db(temporary)
            self.assertEqual(rebuilt["decisions"], 2)
            self.assertEqual(rebuilt["decision_artifacts"], 1)
            self.assertEqual(check_project_projection_db(temporary)["check_status"], "passed")

            projected = read_projected_decisions(temporary, statuses={"active"})
            self.assertEqual(projected["projection_source"], "db")
            self.assertEqual(
                [item["decision_id"] for item in projected["decisions"]],
                ["DEC-execute", "DEC-root"],
            )
            graph = read_projected_decision_graph(temporary, "DEC-root")
            self.assertEqual(
                graph["decision_graph"]["children"][0]["decision"]["decision_id"],
                "DEC-execute",
            )

            project_projection_db_path(temporary).unlink()
            fallback = read_projected_decisions(temporary)
            self.assertEqual(fallback["projection_source"], "files")
            self.assertEqual(fallback["projection_status"], "missing")
            self.assertEqual(len(fallback["decisions"]), 2)

    def test_projection_detects_a_new_decision_revision_as_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = DecisionLedger.create(temporary)
            root = self._decision()
            ledger.append_decision(root)
            rebuild_project_projection_db(temporary)

            completed = copy.deepcopy(root)
            completed["revision"] = 2
            completed["status"] = "completed"
            completed["previous_revision_sha256"] = decision_record_sha256(root)
            ledger.append_decision(completed)

            check = check_project_projection_db(temporary)
            self.assertEqual(check["check_status"], "failed")
            self.assertIn("decision_digest", check["mismatches"])


if __name__ == "__main__":
    unittest.main()

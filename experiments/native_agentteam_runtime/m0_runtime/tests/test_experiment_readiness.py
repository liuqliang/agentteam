import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import agentteam_runtime.agentteam as agentteam_module
import agentteam_runtime.experiment_readiness as readiness_module
from agentteam_runtime.experiment_readiness import (
    ExperimentReadinessError,
    P0_CAPABILITY_IDS,
    _check_pilot_authorization_with_record,
    build_p0_readiness_summary,
    canonical_json_sha256,
    check_pilot_authorization,
    packaged_readiness_record_path,
    phase1_completion_promotion_path,
    validate_experiment_manifest,
    validate_phase1_completion_promotion,
)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest(readiness_sha256):
    return {
        "schema_version": "agentteam_experiment_manifest.v1",
        "experiment_id": "p0-calibration",
        "instance_id": "fixture-001",
        "repository": {
            "source": "/tmp/repository.git",
            "commit": "1" * 40,
            "git_object_format": "sha1",
        },
        "goal": {
            "summary": "Validate the future experiment contract.",
            "constraints": ["Do not read a gold patch."],
        },
        "acceptance": {
            "command": ["python3", "-m", "unittest"],
        },
        "mode": "agentteam_full",
        "runtime": {
            "backend": "codex",
            "model": "codex-test-model",
            "sandbox_policy": "workspace-write",
        },
        "seed": 7,
        "blind_gold": {
            "policy": "unavailable_to_runtime",
        },
        "budgets": {
            "max_total_tokens": 100000,
            "max_wall_time_seconds": 1800,
            "stop_boundary": "scheduler_safe",
        },
        "usage_contract_version": "model_invocation_usage.v1",
        "readiness_binding": {
            "schema_version": "p0_experiment_readiness.v1",
            "record_sha256": readiness_sha256,
        },
    }


class ExperimentReadinessTests(unittest.TestCase):
    def test_git_installed_release_resolves_local_source_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_repo = tmp_path / "source"
            release_root = tmp_path / "release"
            module_path = (
                release_root
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
                / "experiment_readiness.py"
            )
            source_repo.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(source_repo)],
                check=True,
            )
            _write_json(
                release_root / "manifest.json",
                {
                    "install_method": "git_ref",
                    "source_repo": str(source_repo),
                },
            )

            with patch.object(readiness_module, "__file__", str(module_path)):
                resolved = readiness_module._phase1_source_checkout()

            self.assertEqual(resolved, source_repo.resolve())

    def test_packaged_readiness_record_is_truthful_and_complete(self):
        summary = build_p0_readiness_summary()

        self.assertEqual(summary["capability_count"], 7)
        expected_counts = {"passed": 0, "partial": 0, "missing": 0}
        for capability in summary["capabilities"]:
            expected_counts[capability["status"]] += 1
        self.assertEqual(
            summary["capability_counts"],
            expected_counts,
        )
        self.assertEqual(
            {item["capability_id"] for item in summary["capabilities"]},
            set(P0_CAPABILITY_IDS),
        )
        all_passed = expected_counts == {
            "passed": summary["capability_count"],
            "partial": 0,
            "missing": 0,
        }
        self.assertEqual(
            summary["readiness_status"],
            "passed" if all_passed else "blocked",
        )
        self.assertEqual(summary["pilot_authorized"], all_passed)
        self.assertEqual(
            len(summary["blockers"]),
            expected_counts["partial"] + expected_counts["missing"],
        )
        self.assertEqual(summary["phase1_completion"]["promotion_status"], "passed")

    def test_phase1_completion_promotion_binds_finalization_and_approval(self):
        promotion = validate_phase1_completion_promotion()

        self.assertEqual(promotion["promotion_status"], "passed")
        self.assertEqual(
            promotion["source_integration_commit"],
            "ec64cd89e25d88a6a47231f4654ae70c5e7ea148",
        )
        self.assertEqual(promotion["gate_epoch"], 8)

        receipt = _read_json(phase1_completion_promotion_path())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "promotion.json"
            receipt["finalization_artifact"]["sha256"] = "0" * 64
            _write_json(path, receipt)
            with self.assertRaises(ExperimentReadinessError):
                validate_phase1_completion_promotion(path)

            receipt = _read_json(phase1_completion_promotion_path())
            receipt["source_tree"] = "0" * 40
            _write_json(path, receipt)
            with self.assertRaises(ExperimentReadinessError):
                validate_phase1_completion_promotion(path)

            receipt = _read_json(phase1_completion_promotion_path())
            receipt["remote_default_head"] = "0" * 40
            _write_json(path, receipt)
            with self.assertRaises(ExperimentReadinessError):
                validate_phase1_completion_promotion(path)

    def test_readiness_rejects_inventory_and_state_inconsistency(self):
        source = _read_json(packaged_readiness_record_path())
        cases = {
            "duplicate": lambda value: value["capabilities"].__setitem__(
                1,
                dict(value["capabilities"][0]),
            ),
            "unknown": lambda value: value["capabilities"][0].__setitem__(
                "capability_id",
                "unknown_capability",
            ),
            "missing": lambda value: value["capabilities"].pop(),
            "extra": lambda value: value["capabilities"].append(
                dict(value["capabilities"][0])
            ),
            "overall-mismatch": lambda value: value.__setitem__(
                "overall_status",
                "blocked" if value["overall_status"] == "passed" else "passed",
            ),
            "authorization-mismatch": lambda value: value.__setitem__(
                "pilot_authorized",
                not value["pilot_authorized"],
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                record = json.loads(json.dumps(source))
                mutate(record)
                path = Path(tmp) / "readiness.json"
                _write_json(path, record)

                with self.assertRaises(ExperimentReadinessError):
                    build_p0_readiness_summary(path)

    def test_manifest_validation_checks_git_object_format(self):
        readiness = build_p0_readiness_summary()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            manifest = _manifest(readiness["record_sha256"])
            _write_json(path, manifest)

            accepted = validate_experiment_manifest(path)
            self.assertEqual(accepted["manifest_status"], "accepted")
            self.assertEqual(accepted["mode"], "agentteam_full")

            manifest["repository"]["commit"] = "2" * 64
            _write_json(path, manifest)
            with self.assertRaises(ExperimentReadinessError):
                validate_experiment_manifest(path)

    def test_current_readiness_blocks_structurally_valid_pilot(self):
        readiness = build_p0_readiness_summary()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            _write_json(path, _manifest(readiness["record_sha256"]))

            result = check_pilot_authorization(path)

            self.assertEqual(
                result["pilot_authorized"],
                readiness["pilot_authorized"],
            )
            self.assertEqual(result["blockers"], readiness["blockers"])
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(result["target_mutations"], 0)

    def test_all_passed_record_and_matching_digest_authorize_future_path(self):
        record = _read_json(packaged_readiness_record_path())
        for capability in record["capabilities"]:
            capability["status"] = "passed"
            capability["reason"] = "Synthetic unit-test completion evidence."
        record["overall_status"] = "passed"
        record["pilot_authorized"] = True
        record["next_required_phase"] = "P0 calibration"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            record_path = tmp_path / "readiness.json"
            manifest_path = tmp_path / "manifest.json"
            _write_json(record_path, record)
            record_summary = build_p0_readiness_summary(record_path)
            _write_json(
                manifest_path,
                _manifest(record_summary["record_sha256"]),
            )

            result = _check_pilot_authorization_with_record(
                manifest_path,
                record_path,
            )

            self.assertTrue(result["pilot_authorized"])
            self.assertEqual(result["blockers"], [])
            self.assertEqual(result["provider_calls"], 0)
            self.assertEqual(result["target_mutations"], 0)

    def test_manifest_cannot_bind_a_tampered_readiness_digest(self):
        record = _read_json(packaged_readiness_record_path())
        for capability in record["capabilities"]:
            capability["status"] = "passed"
            capability["reason"] = "Synthetic unit-test completion evidence."
        record["overall_status"] = "passed"
        record["pilot_authorized"] = True
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            record_path = tmp_path / "readiness.json"
            manifest_path = tmp_path / "manifest.json"
            _write_json(record_path, record)
            stale_digest = canonical_json_sha256(record)
            record["next_required_phase"] = "tampered after binding"
            _write_json(record_path, record)
            _write_json(manifest_path, _manifest(stale_digest))

            result = _check_pilot_authorization_with_record(
                manifest_path,
                record_path,
            )

            self.assertFalse(result["pilot_authorized"])
            self.assertEqual(
                result["blockers"][-1]["capability_id"],
                "readiness_record_binding",
            )

    def test_cli_reports_readiness_validates_manifest_and_blocks_pilot(self):
        readiness = build_p0_readiness_summary()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = tmp_path / "manifest.json"
            _write_json(manifest_path, _manifest(readiness["record_sha256"]))
            before = sorted(path.name for path in tmp_path.iterdir())

            readiness_stdout = io.StringIO()
            with redirect_stdout(readiness_stdout):
                readiness_rc = agentteam_module.main(
                    ["experiment", "readiness", "--json"]
                )
            validate_stdout = io.StringIO()
            with redirect_stdout(validate_stdout):
                validate_rc = agentteam_module.main(
                    [
                        "experiment",
                        "validate-manifest",
                        "--manifest",
                        str(manifest_path),
                        "--json",
                    ]
                )
            pilot_stdout = io.StringIO()
            pilot_stderr = io.StringIO()
            with redirect_stdout(pilot_stdout), redirect_stderr(pilot_stderr):
                pilot_rc = agentteam_module.main(
                    [
                        "experiment",
                        "check-pilot",
                        "--manifest",
                        str(manifest_path),
                        "--json",
                    ]
                )

            self.assertEqual(readiness_rc, 0)
            self.assertEqual(
                json.loads(readiness_stdout.getvalue())["pilot_authorized"],
                readiness["pilot_authorized"],
            )
            self.assertEqual(validate_rc, 0)
            self.assertEqual(
                json.loads(validate_stdout.getvalue())["manifest_status"],
                "accepted",
            )
            self.assertEqual(pilot_rc, 0 if readiness["pilot_authorized"] else 1)
            pilot_payload = json.loads(
                pilot_stdout.getvalue()
                if readiness["pilot_authorized"]
                else pilot_stderr.getvalue()
            )
            self.assertEqual(
                pilot_payload["pilot_authorized"],
                readiness["pilot_authorized"],
            )
            self.assertEqual(pilot_payload["provider_calls"], 0)
            self.assertEqual(pilot_payload["target_mutations"], 0)
            self.assertEqual(before, sorted(path.name for path in tmp_path.iterdir()))

    def test_pursue_help_does_not_claim_budget_enforcement(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = agentteam_module.main(["help", "pursue"])

        self.assertEqual(result, 0)
        text = stdout.getvalue()
        self.assertNotIn("budget limits", text)
        self.assertIn("not available until P0-B", text)

    def test_cli_routes_deterministic_calibration_request(self):
        summary = {
            "calibration_status": "passed",
            "target_source_commit": "a" * 40,
            "runtime_source_commit": "b" * 40,
            "report_sha256": "c" * 64,
            "report_path": "/tmp/calibration.json",
        }
        stdout = io.StringIO()
        with patch.object(
            agentteam_module,
            "run_deterministic_calibration_from_manifest",
            return_value=summary,
        ) as calibrate, redirect_stdout(stdout):
            result = agentteam_module.main(
                [
                    "experiment",
                    "calibrate-deterministic",
                    "--request",
                    "request.json",
                    "--output",
                    "calibration.json",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), summary)
        calibrate.assert_called_once_with(
            "request.json",
            output_path="calibration.json",
        )


if __name__ == "__main__":
    unittest.main()

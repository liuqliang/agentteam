import copy
import unittest

from agentteam_runtime.phase3_pilot_preparation import (
    FIXED_DATASET_ARTIFACT_SHA256,
    FIXED_DATASET_ROW_COUNT,
    FIXED_SOURCE_COMMIT,
    Phase3PreparationError,
    convert_swe_evo_inventory,
    fixed_dataset_binding,
    routing_manifest_bytes,
    validate_routing_manifest,
)
from agentteam_runtime.experiment_contract import canonical_json_sha256


def _inventory():
    return {
        **fixed_dataset_binding(),
        "instances": [
            {
                "instance_id": f"swe-evo-{index:03d}",
                "repository": "org/repo",
                "complexity_stratum": "lower",
                "language": "python",
                "tags": ["pilot"],
            }
            for index in range(FIXED_DATASET_ROW_COUNT)
        ],
    }


class Phase3PilotPreparationTests(unittest.TestCase):
    def test_fixed_binding_and_byte_stable_manifest(self):
        first = convert_swe_evo_inventory(_inventory())
        second = convert_swe_evo_inventory(copy.deepcopy(_inventory()))
        self.assertEqual(first, second)
        self.assertEqual(routing_manifest_bytes(first), routing_manifest_bytes(second))
        self.assertEqual(first["manifest"]["dataset"]["source_commit"], FIXED_SOURCE_COMMIT)
        self.assertEqual(first["manifest"]["dataset"]["artifact_sha256"], FIXED_DATASET_ARTIFACT_SHA256)

    def test_evaluator_fields_rejected(self):
        for field in ("patch", "test_patch", "score", "prior_result"):
            inventory = _inventory()
            inventory["instances"][0][field] = "restricted"
            with self.subTest(field=field), self.assertRaisesRegex(
                Phase3PreparationError,
                "non-routing",
            ):
                convert_swe_evo_inventory(inventory)

    def test_fixed_binding_mismatch_rejected(self):
        inventory = _inventory()
        inventory["source_commit"] = "0" * 40
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "fixed inventory binding mismatch",
        ):
            convert_swe_evo_inventory(inventory)

    def test_fixed_row_count_mismatch_rejected(self):
        inventory = _inventory()
        inventory["instances"].pop()
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "fixed inventory row count mismatch",
        ):
            convert_swe_evo_inventory(inventory)

    def test_unknown_top_level_fields_rejected(self):
        inventory = _inventory()
        inventory["score"] = 0
        with self.assertRaisesRegex(Phase3PreparationError, "non-routing"):
            convert_swe_evo_inventory(inventory)

    def test_manifest_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["gold_visibility"] = "runtime_visible"
        with self.assertRaises(Phase3PreparationError):
            validate_routing_manifest(manifest)

    def test_resigned_dataset_binding_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["dataset"]["source_repository"] = "https://example.invalid/repo.git"
        manifest["manifest_sha256"] = canonical_json_sha256(manifest["manifest"])
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "dataset binding|schema validation",
        ):
            validate_routing_manifest(manifest)

    def test_resigned_allowlist_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["allowlisted_fields"].append("score")
        manifest["manifest_sha256"] = canonical_json_sha256(manifest["manifest"])
        with self.assertRaises(Phase3PreparationError):
            validate_routing_manifest(manifest)


if __name__ == "__main__":
    unittest.main()

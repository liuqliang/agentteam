import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agentteam_runtime.benchmark_adapter import (
    BenchmarkAdapterError,
    build_benchmark_instance_selection,
    selection_digest,
    validate_benchmark_instance_selection,
    validate_swe_evo_metadata,
)
from agentteam_runtime.experiment_contract import canonical_json_bytes


def _instance(instance_id, repository, stratum, language="python", tags=None):
    return {
        "instance_id": instance_id,
        "repository": repository,
        "complexity_stratum": stratum,
        "language": language,
        "tags": list(tags or ["pilot"]),
    }


def _metadata(revision="swe-evo-metadata-r1"):
    return {
        "schema_version": "swe_evo_metadata.v1",
        "benchmark": "swe_evo",
        "metadata_revision": revision,
        "instances": [
            _instance("low-1", "org/alpha", "low"),
            _instance("low-2", "org/alpha", "low"),
            _instance("low-3", "org/beta", "low", tags=["pilot", "extra"]),
            _instance("high-1", "org/alpha", "high"),
            _instance("high-2", "org/alpha", "high"),
            _instance("high-3", "org/beta", "high", language="rust"),
        ],
    }


def _filters():
    return {
        "repositories": ["org/alpha"],
        "languages": ["python"],
        "required_tags": ["pilot"],
        "excluded_instance_ids": [],
    }


def _select(metadata=None, **overrides):
    arguments = {
        "metadata_revision": "swe-evo-metadata-r1",
        "filters": _filters(),
        "seed": 20260809,
        "stratum_quotas": {"low": 1, "high": 1},
    }
    arguments.update(overrides)
    return build_benchmark_instance_selection(metadata or _metadata(), **arguments)


class BenchmarkAdapterTests(unittest.TestCase):
    def test_selection_schema_is_a_valid_draft_2020_12_schema(self):
        path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "benchmark_instance_selection.schema.json"
        )
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

    def test_equal_inputs_produce_byte_identical_ordered_selection(self):
        first = _select()
        second = _select()

        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(
            first["ordered_instance_ids"],
            [item["instance_id"] for item in first["ordered_instances"]],
        )
        self.assertEqual(
            [item["complexity_stratum"] for item in first["ordered_instances"]],
            ["high", "low"],
        )
        self.assertEqual(first["eligible_counts_by_stratum"], {"high": 2, "low": 2})
        self.assertEqual(
            validate_benchmark_instance_selection(first, metadata=_metadata()),
            first,
        )

    def test_seed_filter_revision_quota_and_metadata_bind_the_digest(self):
        baseline = _select()
        variants = []
        variants.append(_select(seed=20260810))

        filters = _filters()
        filters["excluded_instance_ids"] = [baseline["ordered_instance_ids"][0]]
        variants.append(_select(filters=filters))

        revised = _metadata("swe-evo-metadata-r2")
        variants.append(
            _select(
                revised,
                metadata_revision="swe-evo-metadata-r2",
            )
        )
        variants.append(_select(stratum_quotas={"low": 2, "high": 1}))

        changed_metadata = _metadata()
        changed_metadata["instances"][0]["tags"].append("reviewed")
        variants.append(_select(changed_metadata))

        for variant in variants:
            self.assertNotEqual(
                baseline["selection_sha256"],
                variant["selection_sha256"],
            )

    def test_filter_arrays_are_canonical_sets(self):
        filters = {
            "repositories": ["org/beta", "org/alpha"],
            "languages": ["rust", "python"],
            "required_tags": ["pilot"],
            "excluded_instance_ids": [],
        }
        selection = _select(filters=filters)
        self.assertEqual(selection["filters"]["repositories"], ["org/alpha", "org/beta"])
        self.assertEqual(selection["filters"]["languages"], ["python", "rust"])

    def test_fixed_revision_and_complete_strata_fail_closed(self):
        with self.assertRaisesRegex(BenchmarkAdapterError, "fixed expected revision"):
            _select(metadata_revision="another-revision")

        with self.assertRaisesRegex(BenchmarkAdapterError, "quota requires"):
            _select(stratum_quotas={"low": 1, "medium": 1, "high": 1})

    def test_selection_digest_and_semantics_detect_mutation(self):
        selection = _select()
        mutated = copy.deepcopy(selection)
        mutated["ordered_instance_ids"].reverse()
        with self.assertRaisesRegex(BenchmarkAdapterError, "selection_sha256"):
            validate_benchmark_instance_selection(mutated)

        mutated["selection_sha256"] = selection_digest(mutated)
        with self.assertRaisesRegex(BenchmarkAdapterError, "must match"):
            validate_benchmark_instance_selection(mutated)

    def test_validator_returns_a_detached_snapshot(self):
        metadata = _metadata()
        validated = validate_swe_evo_metadata(
            metadata,
            expected_revision="swe-evo-metadata-r1",
        )
        metadata["instances"][0]["tags"].append("mutated")
        self.assertEqual(validated["instances"][0]["tags"], ["pilot"])

    def test_gold_patch_is_rejected_at_top_level_and_instance_level(self):
        top_level = _metadata()
        top_level["gold_patch"] = "restricted"
        with self.assertRaisesRegex(BenchmarkAdapterError, "forbidden gold"):
            validate_swe_evo_metadata(top_level)

        instance_level = _metadata()
        instance_level["instances"][0]["reference_patch"] = "restricted"
        with self.assertRaisesRegex(BenchmarkAdapterError, "forbidden gold"):
            validate_swe_evo_metadata(instance_level)

    def test_prior_outcomes_and_scores_are_rejected(self):
        for forbidden_field in ("prior_outcome", "prior_run_result", "score"):
            with self.subTest(field=forbidden_field):
                metadata = _metadata()
                metadata["instances"][0][forbidden_field] = "restricted"
                with self.assertRaisesRegex(BenchmarkAdapterError, "prior-outcome"):
                    validate_swe_evo_metadata(metadata)

    def test_unknown_metadata_and_filter_fields_are_rejected(self):
        metadata = _metadata()
        metadata["task_text"] = "not part of selection metadata"
        with self.assertRaisesRegex(BenchmarkAdapterError, "allowlist"):
            validate_swe_evo_metadata(metadata)

        filters = _filters()
        filters["observed_successes"] = ["low-1"]
        with self.assertRaisesRegex(BenchmarkAdapterError, "forbidden gold"):
            _select(filters=filters)

    def test_duplicate_ids_and_non_json_values_are_rejected(self):
        metadata = _metadata()
        metadata["instances"].append(copy.deepcopy(metadata["instances"][0]))
        with self.assertRaisesRegex(BenchmarkAdapterError, "duplicate instance_id"):
            validate_swe_evo_metadata(metadata)

        metadata = _metadata()
        metadata["instances"][0]["tags"] = {"pilot"}
        with self.assertRaisesRegex(BenchmarkAdapterError, "canonical JSON"):
            validate_swe_evo_metadata(metadata)


if __name__ == "__main__":
    unittest.main()

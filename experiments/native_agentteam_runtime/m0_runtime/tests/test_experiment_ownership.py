import inspect
import unittest

from tests import test_experiment_harness as cases
from tests import experiment_suite_budget
from tests import experiment_suite_calibration
from tests import experiment_suite_results
from tests import experiment_suite_runtime


class ExperimentOwnershipTests(unittest.TestCase):
    def test_every_harness_case_class_has_exactly_one_discovery_owner(self):
        discovered = {
            name
            for name, value in vars(cases).items()
            if inspect.isclass(value)
            and issubclass(value, unittest.TestCase)
            and value is not unittest.TestCase
            and value.__module__ == cases.__name__
        }
        assignments = (
            *experiment_suite_budget.CASE_CLASSES,
            *experiment_suite_results.CASE_CLASSES,
            *experiment_suite_runtime.CASE_CLASSES,
            *experiment_suite_calibration.CASE_CLASSES,
        )

        self.assertEqual(set(assignments), discovered)
        self.assertEqual(len(assignments), len(set(assignments)))


if __name__ == "__main__":
    unittest.main()

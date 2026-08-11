from tests import test_experiment_harness as cases
from tests.experiment_test_ownership import suite_for_case_classes


CASE_CLASSES = (
    "ExperimentResultBundleTests",
    "ExperimentModeAdapterTests",
    "ExperimentContractSchemaTests",
)


def load_tests(loader, _tests, _pattern):
    return suite_for_case_classes(loader, cases, CASE_CLASSES)

import unittest

from tests import test_m0_runtime
from tests import test_taskpack


def _owned_test_methods(case_class):
    ownership = {}
    for base in case_class.__bases__:
        for name, value in vars(base).items():
            if name.startswith("test_") and callable(value):
                ownership.setdefault(name, []).append(base.__name__)
    return ownership


class MonolithOwnershipTests(unittest.TestCase):
    def test_m0_runtime_methods_have_exactly_one_domain_owner(self):
        ownership = _owned_test_methods(test_m0_runtime.M0RuntimeTests)

        self.assertTrue(ownership)
        self.assertFalse(
            {name: owners for name, owners in ownership.items() if len(owners) != 1}
        )
        self.assertFalse(
            {
                name
                for name in vars(test_m0_runtime.M0RuntimeTests)
                if name.startswith("test_")
            }
        )

    def test_taskpack_methods_have_exactly_one_domain_owner(self):
        ownership = _owned_test_methods(test_taskpack.TaskpackTests)

        self.assertTrue(ownership)
        self.assertFalse(
            {name: owners for name, owners in ownership.items() if len(owners) != 1}
        )
        self.assertFalse(
            {
                name
                for name in vars(test_taskpack.TaskpackTests)
                if name.startswith("test_")
            }
        )


if __name__ == "__main__":
    unittest.main()

import unittest


def suite_for_case_classes(loader, case_module, class_names):
    suite = unittest.TestSuite()
    for class_name in class_names:
        suite.addTests(loader.loadTestsFromTestCase(getattr(case_module, class_name)))
    return suite

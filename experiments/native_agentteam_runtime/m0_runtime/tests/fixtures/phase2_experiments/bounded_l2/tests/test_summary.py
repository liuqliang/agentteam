import unittest

from src.summary import totals


class SummaryTests(unittest.TestCase):
    def test_normalizes_empty_label(self):
        records = [{"label": "", "value": "2"}, {"value": 3}]
        self.assertEqual(totals(records), {"unknown": 5})

    def test_does_not_mutate_input(self):
        records = [{"label": "a", "value": "2"}]
        totals(records)
        self.assertEqual(records, [{"label": "a", "value": "2"}])


if __name__ == "__main__":
    unittest.main()

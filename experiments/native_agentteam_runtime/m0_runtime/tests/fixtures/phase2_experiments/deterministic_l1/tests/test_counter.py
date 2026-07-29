import unittest

from src.counter import increment


class CounterTests(unittest.TestCase):
    def test_increment(self):
        self.assertEqual(increment(2), 3)


if __name__ == "__main__":
    unittest.main()

import unittest

from durations.parser import parse_duration
from durations.formatting import format_duration
class Contract(unittest.TestCase):
    def test_round_trip(self):
        for seconds in (0, 1, 60, 3600, 3661, 7322):
            self.assertEqual(parse_duration(format_duration(seconds)), seconds)
    def test_order(self):
        with self.assertRaises(ValueError): parse_duration('1s2m')


if __name__ == '__main__':
    unittest.main()

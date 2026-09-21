import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_adds_two_numbers(self) -> None:
        expected = 5
        assert add(2, 3) == expected


if __name__ == "__main__":
    unittest.main()

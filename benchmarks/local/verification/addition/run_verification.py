"""Held-out addition checks copied into the agent workspace only after its run."""

from __future__ import annotations

import unittest

from calc import add


class AdditionContract(unittest.TestCase):
    def test_positive(self) -> None:
        self.assertEqual(add(7, 5), 12)

    def test_negative(self) -> None:
        self.assertEqual(add(-4, -6), -10)

    def test_zero(self) -> None:
        self.assertEqual(add(0, 9), 9)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AdditionContract)
    result = unittest.TextTestRunner().run(suite)
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun == 3 else 1)

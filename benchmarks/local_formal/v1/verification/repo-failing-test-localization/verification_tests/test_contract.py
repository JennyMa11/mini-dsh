import unittest

from ledger.ledger import Ledger
class Contract(unittest.TestCase):
    def test_signed(self):
        ledger = Ledger(); ledger.add(8); ledger.add(-11)
        self.assertEqual(ledger.balance(), -3)
    def test_zero(self): self.assertEqual(Ledger().balance(), 0)


if __name__ == '__main__':
    unittest.main()

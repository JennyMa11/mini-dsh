import unittest

from billing.discounts import apply_discount, apply_surcharge
from billing.checkout import checkout_total
class Contract(unittest.TestCase):
    def test_zero(self): self.assertEqual(apply_discount(10, 0), 10)
    def test_hundred(self): self.assertEqual(apply_discount(10, 100), 0)
    def test_surcharge(self): self.assertEqual(apply_surcharge(10, 10), 11)
    def test_callsite(self): self.assertEqual(checkout_total(80), 68)


if __name__ == '__main__':
    unittest.main()

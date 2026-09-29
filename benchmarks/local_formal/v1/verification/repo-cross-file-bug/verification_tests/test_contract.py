import unittest

from shop.orders import order_total
from shop.pricing import bulk_discount
class Contract(unittest.TestCase):
    def test_boundary(self): self.assertEqual(bulk_discount(10), .10)
    def test_below(self): self.assertEqual(bulk_discount(9), 0)
    def test_total(self): self.assertEqual(order_total(2, 10, 'jp'), 19.8)


if __name__ == '__main__':
    unittest.main()

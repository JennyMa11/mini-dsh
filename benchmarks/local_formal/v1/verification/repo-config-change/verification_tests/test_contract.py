import unittest

from service.settings import load
from service.client import describe
class Contract(unittest.TestCase):
    def test_prod(self): self.assertEqual(describe('prod'), 'prod:5x30s')
    def test_unknown(self):
        with self.assertRaises(ValueError): load('unknown')


if __name__ == '__main__':
    unittest.main()

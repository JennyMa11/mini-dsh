import unittest

from textkit.normalize import normalize
from textkit.slugify import slugify
class Contract(unittest.TestCase):
    def test_preserve_case(self): self.assertEqual(normalize(' A  B '), 'A B')
    def test_slug_case(self): self.assertEqual(slugify(' A  B '), 'a-b')


if __name__ == '__main__':
    unittest.main()

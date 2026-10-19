import unittest

from practice import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercases_and_joins_words(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_empty_text_gives_an_empty_slug(self):
        self.assertEqual(slugify("  "), "")


if __name__ == "__main__":
    unittest.main()

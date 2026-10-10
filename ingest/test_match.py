import unittest

from match import score, transliterate_to_cyrillic, queries, normalize


class MatchTests(unittest.TestCase):
    def test_translit(self):
        self.assertEqual(transliterate_to_cyrillic("Grisha Urgant"), "гриша ургант")

    def test_queries_add_cyrillic_artist_for_latin(self):
        self.assertIn("гриша ургант Драма", queries("Драма", "Grisha Urgant", "Гостелерадио"))

    def test_normalize_yo_and_punct(self):
        self.assertEqual(normalize("Ёлка, (live)"), "елка live")

    def test_accepts_cyrillic_artist_with_close_duration(self):
        r = {"videoId": "v", "title": "Драма", "duration_seconds": 243, "artists": [{"name": "Гриша Ургант"}]}
        self.assertIsNotNone(score("Драма", "Grisha Urgant", 242, r))

    def test_rejects_wrong_duration(self):
        r = {"videoId": "v", "title": "Зима навсегда", "duration_seconds": 253, "artists": [{"name": "Гриша Ургант"}]}
        self.assertIsNone(score("Зима навсегда", "Grisha Urgant", 162, r))

    def test_rejects_different_title(self):
        r = {"videoId": "v", "title": "Совсем другая песня", "duration_seconds": 242, "artists": []}
        self.assertIsNone(score("Драма", "Grisha Urgant", 242, r))


if __name__ == "__main__":
    unittest.main()

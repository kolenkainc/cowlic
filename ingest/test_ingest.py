import unittest

import ingest

SONG = {"name": "T", "artists": ["A"], "album_artist": "A", "album_name": "Al", "year": 2020,
        "song_id": "sp1", "track_number": 3, "disc_number": 1, "duration": 200}


class IngestTest(unittest.TestCase):
    def test_ids_stable_and_distinct(self):
        self.assertEqual(ingest.artist_id("Foo"), ingest.artist_id(" foo "))
        self.assertNotEqual(ingest.artist_id("Foo"), ingest.album_id("Foo", "Foo"))
        self.assertNotEqual(ingest.album_id("a", "bc"), ingest.album_id("ab", "c"))
        self.assertEqual(len(ingest.track_id("x")), 16)

    def test_size_filter(self):
        self.assertFalse(ingest.too_big(20 * 1024 * 1024))
        self.assertTrue(ingest.too_big(20 * 1024 * 1024 + 1))

    def test_sql_is_parameterized(self):
        stmts = ingest.upsert_statements(SONG, 1000, "fid", 7, "cov")
        self.assertEqual(len(stmts), 3)
        for sql, params in stmts:
            self.assertEqual(sql.count("?"), len(params))
            self.assertNotIn("Al", sql)
        self.assertIn("COALESCE", stmts[1][0])
        self.assertEqual(stmts[2][1][0], ingest.track_id("sp1"))

    def test_largest_photo(self):
        photos = [{"file_id": "s", "file_size": 10, "width": 1, "height": 1},
                  {"file_id": "b", "file_size": 99, "width": 2, "height": 2}]
        self.assertEqual(ingest.largest_photo_file_id(photos), "b")


if __name__ == "__main__":
    unittest.main()

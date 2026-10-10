import tempfile
import unittest
from pathlib import Path

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
            self.assertNotIn("Al", sql)  # values travel only as params
        # statement text is constant, independent of hostile input
        evil = dict(SONG, name="x'); DROP TABLE tracks;--", album_name="a'--", song_id="sp1")
        self.assertEqual([s for s, _ in ingest.upsert_statements(evil, 1, "f", 1, None)], [s for s, _ in stmts])
        self.assertIn("COALESCE", stmts[1][0])
        self.assertEqual(stmts[2][1][0], ingest.track_id("sp1"))

    def test_largest_photo(self):
        photos = [{"file_id": "s", "file_size": 10, "width": 1, "height": 1},
                  {"file_id": "b", "file_size": 99, "width": 2, "height": 2}]
        self.assertEqual(ingest.largest_photo_file_id(photos), "b")

    def test_missing_artist_and_album_tolerated(self):
        stmts = ingest.upsert_statements({"name": "T", "artists": [], "song_id": "sp1", "duration": 5}, 1, "f", 1, None)
        self.assertEqual(stmts[0][1][1], ingest.UNKNOWN_ARTIST)
        self.assertEqual(stmts[1][1][2], ingest.UNKNOWN_ALBUM)

    def test_dedupe_and_redact(self):
        self.assertEqual(len(ingest.dedupe([{"song_id": "a"}, {"song_id": "a"}, {"song_id": "b"}])), 2)
        self.assertEqual(ingest.redact("id=SEC1 x", ["SEC1", ""]), "id=*** x")


def sid(n):
    return f"{n:022d}"


class Fakes:
    def __init__(self, tmp, no_match=(), big=(), fail_upload=(), fail_d1=()):
        self.tmp, self.no_match, self.big, self.fail_upload, self.fail_d1 = tmp, set(no_match), set(big), \
            set(fail_upload), set(fail_d1)
        self.uploaded, self.deleted, self.batches = [], [], []

    def deps(self):
        def download(song, m, tdir):
            p = tdir / "a.mp3"
            p.write_bytes(b"x" * (ingest.MAX_BYTES + 1 if song["song_id"] in self.big else 10))
            return p

        def audio(song, path):
            if song["song_id"] in self.fail_upload:
                raise ingest.IngestError("Telegram API error 429: flood")
            self.uploaded.append(song["song_id"])
            return {"message_id": len(self.uploaded), "audio": {"file_id": "f"}}

        def d1(stmts):
            if stmts[2][1][1] in self.fail_d1:
                raise ingest.IngestError("D1 API error 500")
            self.batches.append(stmts)

        return ingest.Deps(lambda s: None if s["song_id"] in self.no_match else object(), download, audio,
                           lambda s, d: "cover", d1, self.deleted.append)


def song(n, **kw):
    return dict(SONG, song_id=sid(n), name=f"T{n}", url="u", **kw)


class RunTest(unittest.TestCase):
    def go(self, songs, existing=(), **kw):
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d), **kw)
            existing = set(existing)
            counts = ingest.run(songs, f.deps(), existing, {}, Path(d), False)
            return f, counts, existing

    def test_skips_existing_and_duplicates(self):
        f, c, ex = self.go([song(1), song(2), song(2)], existing=[sid(1)])
        self.assertEqual((c["new"], c["exists"]), (1, 1))
        self.assertEqual(f.uploaded, [sid(2)])
        self.assertIn(sid(2), ex)

    def test_no_match_and_oversize_are_not_uploaded(self):
        f, c, _ = self.go([song(1), song(2)], no_match=[sid(1)], big=[sid(2)])
        self.assertEqual((c["no_match"], c["too_big"], c["failed"]), (1, 1, 0))
        self.assertEqual(f.uploaded, [])

    def test_failure_does_not_stop_next_track(self):
        f, c, _ = self.go([song(1), song(2)], fail_upload=[sid(1)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))
        self.assertEqual(f.uploaded, [sid(2)])

    def test_d1_failure_deletes_telegram_message_and_continues(self):
        f, c, ex = self.go([song(1), song(2)], fail_d1=[sid(1)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))
        self.assertEqual(f.deleted, [1])
        self.assertNotIn(sid(1), ex)

    def test_bad_song_id_fails_that_track_only(self):
        _, c, _ = self.go([dict(SONG, song_id="../x"), song(2)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))

    def test_missing_fields_fail_only_that_track(self):
        _, c, _ = self.go([{"song_id": sid(1)}, song(2)])
        self.assertEqual(c["new"], 1)


if __name__ == "__main__":
    unittest.main()

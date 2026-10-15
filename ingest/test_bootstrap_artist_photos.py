"""Tests for `bootstrap.py covers --artists` and bootstrap_artist_photos.py (#253). Offline, like test_bootstrap_covers.py
(whose in-memory D1 and base class are reused): the matcher, the artist page, the photo upload and the YouTube Music
client are fakes, every test has its own temp state directory, no .env is read, no network, and every name is invented."""
import datetime
import fcntl
import json
import os
import stat
import unittest
from unittest import mock

import bootstrap
import bootstrap_artist_photos as ap
import bootstrap_covers as bc
import ingest
import match
from test_bootstrap_covers import NOW_LOCAL, SENTINELS, Base, FakeD1

PHOTO_URL = "https://lh3.googleusercontent.com/FAKEARTIST" + match.ARTIST_PHOTO_SUFFIX
BROWSE_ID = "UCfakeartist1"


def cand(artist="Zorblax", artist_id=BROWSE_ID):
    return match.Candidate("vid1", "t", artist, 200, 1.0, album="Quuxnaut Album", artist_id=artist_id, artist_name=artist,
                           thumbnail_base="https://lh3.googleusercontent.com/FAKEBASE")


class FakeArtistBackends:
    """`matches` maps a STORED ARTIST NAME to what find_match returns (a Candidate, None or an exception to raise);
    an artist not listed gets a Candidate that passes the guard. `pages` maps a browse id to the photo URL (or None)."""

    def __init__(self, d1, matches=None, upload_error=None):
        self.d1, self.matches, self.upload_error = d1, matches or {}, upload_error
        self.found: list = []
        self.page_calls: list = []
        self.pages: dict = {}
        self.uploads: list = []

    def find(self, todo):
        self.found.append(todo)
        v = self.matches.get(todo.artist, cand(artist=todo.artist))
        if isinstance(v, Exception):
            raise v
        return v

    def page(self, browse_id):
        self.page_calls.append(browse_id)
        return self.pages.get(browse_id, PHOTO_URL)

    def upload(self, url, tdir):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append(url)
        return f"image-{len(self.uploads)}"

    def backends(self):
        return ap.ArtistBackends(self.find, self.page, self.upload, self.d1.write)


class ArtistBase(Base):
    def setUp(self):
        super().setUp()
        self.afb = FakeArtistBackends(self.d1)
        self.constructed_artist = 0
        p = mock.patch.object(bootstrap, "real_artist_backends", self.make_artist_backends)
        p.start()
        self.addCleanup(p.stop)

    def new_d1(self, **kw):
        return super().new_d1(**{"artists_image_column": True, **kw})

    def make_artist_backends(self):
        self.constructed_artist += 1
        return self.afb.backends()

    def artists(self, *extra, max_new="100", max_minutes="60"):
        return self.main("covers", "--artists", "--max-new", max_new, "--max-minutes", max_minutes, *extra)

    def image(self, name):
        return self.d1.db.execute("SELECT image_file_id FROM artists WHERE id = ?", [ingest.artist_id(name)]).fetchone()[0]

    def state(self) -> dict:
        return json.loads((self.dir / ap.STATE_FILE).read_text())

    def seed(self, *names):
        for n in names:
            self.d1.add(n, "Quuxnaut Album", title="Frobnitz")
        return [ingest.artist_id(n) for n in names]


class FillTest(ArtistBase):
    def test_an_artist_without_an_image_gets_one_with_the_parameterized_update(self):
        (ar,) = self.seed("Zorblax")
        self.assertEqual(self.artists(), 0)
        self.assertEqual(self.image("Zorblax"), "image-1")
        self.assertEqual(self.d1.writes, [(ap.UPDATE_SQL, ["image-1", ar])])
        self.assertEqual(self.afb.page_calls, [BROWSE_ID])  # one get_artist per artist
        self.assertEqual(self.afb.uploads, [PHOTO_URL])
        self.assertIn("photographed 1", self.out)
        for s in SENTINELS:
            self.assertNotIn(s, self.out + self.err)

    def test_the_update_is_the_idempotent_statement(self):
        self.assertEqual(ap.UPDATE_SQL, "UPDATE artists SET image_file_id = ? WHERE id = ? AND image_file_id IS NULL")

    def test_a_stored_image_wins_over_one_written_meanwhile(self):
        self.seed("Zorblax")
        self.afb.upload = lambda url, tdir: (self.d1.db.execute("UPDATE artists SET image_file_id = 'stored'"), "mine")[1]
        self.assertEqual(self.artists(), 0)
        self.assertEqual(self.image("Zorblax"), "stored")
        self.assertIn("already 1", self.out)
        self.assertIn("photographed 0", self.out)

    def test_artists_with_an_image_are_not_selected_and_one_track_per_artist_is_matched(self):
        self.seed("Zorblax", "Wibblewobble")
        self.d1.add("Zorblax", "Second Album", title="Other", tracks=3)
        self.d1.db.execute("UPDATE artists SET image_file_id = 'old' WHERE id = ?", [ingest.artist_id("Wibblewobble")])
        self.artists()
        self.assertEqual([t.artist for t in self.afb.found], ["Zorblax"])
        self.assertEqual(self.image("Wibblewobble"), "old")

    def test_the_unknown_album_is_matched_without_an_album_and_the_duration_zero_means_title_only(self):
        self.d1.add("Zorblax", ingest.UNKNOWN_ALBUM, duration=0)
        self.artists()
        self.assertEqual([(t.album, t.duration_s) for t in self.afb.found], [(None, None)])

    def test_the_cyrillic_variant_of_the_artist_name_is_accepted(self):
        self.seed("Zorblax")
        self.afb.matches["Zorblax"] = cand(artist="Зорблакс")  # an invented name
        self.artists()
        self.assertEqual(self.image("Zorblax"), "image-1")

    def test_the_album_mode_state_file_is_not_touched(self):
        self.seed("Zorblax")
        self.afb.matches["Zorblax"] = None
        self.artists()
        self.assertFalse((self.dir / bc.STATE_FILE).exists())
        self.assertEqual(set(self.state()), {"version", "artists"})


class CooldownTest(ArtistBase):
    def test_a_guard_mismatch_is_not_written_and_rests(self):
        (ar,) = self.seed("Zorblax")
        self.afb.matches["Zorblax"] = cand(artist="Somebody Else Entirely")
        self.assertEqual(self.artists(), 0)
        self.assertIsNone(self.image("Zorblax"))
        self.assertEqual((self.afb.page_calls, self.afb.uploads, self.d1.writes), ([], [], []))
        self.assertIn("artist_mismatch 1", self.out)
        self.assertEqual(self.state()["artists"][ar], {"result": "mismatch", "on": "2026-10-11"})
        self.afb.found.clear()
        self.artists()
        self.assertEqual(self.afb.found, [])  # not retried
        self.assertIn("cooldown 1", self.out)

    def test_a_pseudo_artist_and_a_match_without_an_artist_id_get_no_photo(self):
        self.seed("Various Artists", "Zorblax")
        self.afb.matches["Zorblax"] = cand(artist_id=None)
        self.artists()
        self.assertEqual((self.afb.page_calls, self.d1.writes), ([], []))
        self.assertEqual({e["result"] for e in self.state()["artists"].values()}, {"mismatch"})

    def test_no_match_and_a_page_without_a_thumbnail_rest(self):
        a, b = self.seed("Zorblax", "Wibblewobble")
        self.afb.matches["Zorblax"] = None
        self.afb.pages[BROWSE_ID] = None
        self.artists()
        self.assertEqual({e["result"] for e in self.state()["artists"].values()}, {"no_match", "mismatch"})
        self.assertIn("no_match 1", self.out)
        self.assertIn("no_photo 1", self.out)

    def test_a_failure_rests_for_the_short_failed_days_and_exits_1(self):
        (ar,) = self.seed("Zorblax")
        self.afb.upload_error = ingest.IngestError("Frobnitz secret detail")
        self.assertEqual(self.artists(), 1)
        self.assertIsNone(self.image("Zorblax"))
        self.assertEqual(self.d1.writes, [])
        self.assertEqual(self.state()["artists"][ar]["result"], "failed")
        self.assertNotIn("Frobnitz", self.out + self.err)  # the exception text is never logged
        self.afb.found.clear()
        self.artists()
        self.assertEqual(self.afb.found, [])
        self.artists("--retry-failed-days", "0")
        self.assertEqual(len(self.afb.found), 1)  # 0 disables the cooldown

    def test_three_failures_in_a_row_stop_the_run(self):
        self.seed("Zorblax", "Wibblewobble", "Quuxnaut", "Frobnitz")
        self.afb.upload_error = ingest.IngestError("x")
        self.assertEqual(self.artists(), 1)
        self.assertIn("stop reason: failed", self.out)
        self.assertEqual(len(self.afb.found), 3)

    def test_the_caps_stop_the_run_and_the_next_run_resumes_from_d1(self):
        self.seed("Zorblax", "Wibblewobble", "Quuxnaut")
        self.artists(max_new="2")
        self.assertEqual(len(self.afb.found), 2)
        self.assertIn("stop reason: cap", self.out)
        self.afb.found.clear()
        self.artists()
        self.assertEqual(len(self.afb.found), 1)


class StopsTest(ArtistBase):
    def test_a_sign_in_challenge_exits_3_and_is_not_retried_or_recorded(self):
        self.seed("Zorblax", "Wibblewobble")
        self.afb.matches["Zorblax"] = ingest.YouTubeSignInRequired("bot check")
        self.afb.matches["Wibblewobble"] = ingest.YouTubeSignInRequired("bot check")
        self.assertEqual(self.artists(), 3)
        self.assertEqual(len(self.afb.found), 1)  # the run ends at the first challenge
        self.assertFalse((self.dir / ap.STATE_FILE).exists())
        self.assertEqual(self.d1.writes, [])

    def test_a_challenge_from_the_artist_page_exits_3(self):
        self.seed("Zorblax")

        def page(_):
            raise ingest.YouTubeSignInRequired("bot check")
        self.afb.page = page
        self.assertEqual(self.artists(), 3)
        self.assertEqual(self.d1.writes, [])

    def test_the_lock_the_guard_and_the_caps_are_those_of_covers(self):
        self.seed("Zorblax")
        fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600) if self.dir.exists() else None
        if fd is None:
            self.dir.mkdir(parents=True)
            fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.artists(), bootstrap.EXIT_LOCKED)
        self.assertEqual(self.afb.found, [])

    def test_the_21_10_guard_refuses_before_any_work(self):
        self.seed("Zorblax")
        with mock.patch.object(bootstrap, "local_now", lambda: datetime.datetime(2026, 10, 11, 21, 0, 0)):
            self.assertEqual(self.artists(), bootstrap.EXIT_GUARD)
        self.assertEqual((self.constructed_artist, self.afb.found), (0, []))

    def test_both_caps_are_required_without_count(self):
        self.assertEqual(self.main("covers", "--artists"), bootstrap.EXIT_USAGE)
        self.assertEqual(self.main("covers", "--artists", "--max-new", "5"), bootstrap.EXIT_USAGE)
        self.assertEqual(self.constructed_artist, 0)


class CountTest(ArtistBase):
    def test_count_is_read_only_and_builds_no_backend(self):
        self.seed("Zorblax", "Wibblewobble")
        self.d1.db.execute("UPDATE artists SET image_file_id = 'old' WHERE id = ?", [ingest.artist_id("Wibblewobble")])
        before = self.d1.db.total_changes
        self.assertEqual(self.main("covers", "--artists", "--count"), 0)
        self.assertIn("artists without an image: 1 (to try: 1, in cooldown: 0", self.out)
        self.assertEqual((self.d1.writes, self.constructed_artist, self.afb.found), ([], 0, []))
        self.assertEqual(self.d1.db.total_changes, before)
        self.assertFalse(self.dir.exists() and (self.dir / ap.STATE_FILE).exists())
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql, _ in self.d1.queries))

    def test_count_reports_the_cooldown(self):
        (ar,) = self.seed("Zorblax")
        self.dir.mkdir(parents=True)
        st = ap.ArtistPhotoState(self.dir / ap.STATE_FILE)
        st.record(ar, "mismatch", datetime.date(2026, 10, 10))
        st.save()
        self.main("covers", "--artists", "--count")
        self.assertIn("(to try: 0, in cooldown: 1", self.out)

    def test_count_takes_no_caps(self):
        self.assertEqual(self.main("covers", "--artists", "--count", "--max-new", "3"), bootstrap.EXIT_USAGE)


class LegacySchemaTest(ArtistBase):
    def new_d1(self, **kw):
        return super().new_d1(**{**kw, "artists_image_column": False})

    def test_without_the_column_the_run_is_a_clean_skip(self):
        self.seed("Zorblax")
        self.assertEqual(self.artists(), 0)
        self.assertEqual((self.constructed_artist, self.afb.found, self.d1.writes), (0, [], []))
        self.assertIn("column does not exist", self.out)
        self.assertFalse((self.dir / ap.STATE_FILE).exists())

    def test_without_the_column_the_count_says_so(self):
        self.seed("Zorblax")
        self.assertEqual(self.main("covers", "--artists", "--count"), 0)
        self.assertIn("not counted yet", self.out)

    def test_the_work_list_query_is_never_sent_without_the_column(self):
        self.seed("Zorblax")
        self.artists()
        self.assertFalse(any("image_file_id IS NULL" in sql for sql, _ in self.d1.queries))


class StateFileTest(ArtistBase):
    def test_the_state_file_is_private_and_strict(self):
        (ar,) = self.seed("Zorblax")
        self.afb.matches["Zorblax"] = None
        self.artists()
        path = self.dir / ap.STATE_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.write_text(json.dumps({"version": 1, "albums": {}}))  # the album key is refused: another file's shape
        self.assertEqual(self.artists(), bootstrap.EXIT_USAGE)
        path.write_text(json.dumps({"version": 1, "artists": {"not-a-hash": {"result": "failed", "on": "2026-10-11"}}}))
        self.assertEqual(self.artists(), bootstrap.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()

"""Artist photos from YouTube Music get_artist (#251). Offline: every call, download and upload is a fake (NoNetwork base)."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ingest
import match
from test_covers import BASE, JPEG, cand
from test_ingest import Fakes, SONG, song
from test_lastfm_source import NoNetwork

MIGRATIONS = Path(__file__).resolve().parent.parent / "worker" / "migrations"
PHOTO_URL = "https://yt3.googleusercontent.com/artistBanner" + match.ARTIST_PHOTO_SUFFIX


def lfm(n, artist="The Killers", **kw):
    """A song as the Last.fm source makes it: no Spotify URL and no cover_url."""
    return dict(SONG, song_id=f"{n:022d}", name=f"T{n}", url=None, cover_url=None, album_name="Hot Fuss",
                album_artist=artist, artists=[artist], **kw)


def candidate(artist_id="UCabcdefghij", name="The Killers", joined=None):
    c = cand(artist_name=name, artists=joined or name)
    c.artist_id = artist_id
    return c


class Harness:
    """process_track with fake artist-photo deps; records every call in order."""

    def __init__(self, tmp, images=None, photo_url=PHOTO_URL, fail_get=None, fail_upload=None, loader=None):
        self.tmp, self.calls, self.log = Path(tmp), [], []
        self.fakes = Fakes(self.tmp)
        self.images = {} if images is None else images
        self.loads = 0
        deps = self.fakes.deps()
        deps.find_match = lambda s: candidate()
        deps.upload_cover = lambda s, d: "cov"

        def get(aid):
            self.calls.append(("get_artist", aid))
            if fail_get:
                raise fail_get
            return photo_url

        def up(url, d):
            self.calls.append(("upload", url))
            if fail_upload:
                raise fail_upload
            return "photo1"

        def load():
            self.loads += 1
            return self.images

        deps.artist_images = LazyOf(loader or load)
        deps.artist_photo_url, deps.upload_artist_image = get, up
        self.deps = deps

    def track(self, s, m=None, notes=None, dry=False, existing=None):
        if m is not None:
            self.deps.find_match = lambda _s: m
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = ingest.process_track(s, self.deps, set() if existing is None else existing, {}, self.tmp, dry,
                                          notes)
        self.log.append(out.getvalue())
        return status

    @property
    def artist_stmts(self):
        return [b[0] for b in self.fakes.batches]


def LazyOf(load):
    return ingest.LazyArtistImages(load)


class ArtistGuardTest(NoNetwork):
    def test_suffix_is_the_verified_square_crop(self):
        self.assertEqual(match.ARTIST_PHOTO_SUFFIX, "=w800-h800-p-l90-rj")

    def test_matching_artist_gives_the_id(self):
        self.assertEqual(match.artist_id_from_match("The Killers", candidate()), ("UCabcdefghij", "ok"))

    def test_other_artist_gets_no_id(self):
        self.assertEqual(match.artist_id_from_match("The Killers", candidate(name="Cover Band")), (None, "artist_mismatch"))

    def test_latin_vs_cyrillic_is_accepted(self):
        self.assertEqual(match.artist_id_from_match("Zemfira", candidate(name="Земфира"))[1], "ok")

    def test_leading_article_does_not_decide(self):
        self.assertEqual(match.artist_id_from_match("Killers", candidate())[1], "ok")

    def test_plural_rule_and_bar_are_the_cover_guard(self):
        self.assertEqual(match.artist_id_from_match("Slayer", candidate(name="Slayers"))[1], "artist_mismatch")
        self.assertEqual(match.COVER_ARTIST_MIN_SIMILARITY, 0.8)
        self.assertEqual(match.artist_id_from_match("abcde", candidate(name="abcdx"))[1], "ok")  # ratio 0.8: at the bar
        self.assertEqual(match.artist_id_from_match("abcdefghij", candidate(name="abcdefgxyz"))[1], "artist_mismatch")  # 0.7

    def test_missing_or_odd_ids_are_a_no_op(self):
        for bad in (None, "", 7, "short", "has space  xx", "UC/../x1234567", "a" * 65, "UC;rm -rf x"):
            self.assertEqual(match.artist_id_from_match("The Killers", candidate(artist_id=bad)), (None, "no_artist_id"))

    def test_only_the_first_artist_of_the_result_decides(self):
        """The id belongs to artists[0]; a feature credit first and the track's artist second must not pass."""
        featured_first = candidate(artist_id="UCfeatured00", name="Featured", joined="Featured, Primary")
        self.assertEqual(match.artist_id_from_match("Primary", featured_first), (None, "artist_mismatch"))
        primary_first = candidate(artist_id="UCprimary000", name="Primary", joined="Primary, Featured")
        self.assertEqual(match.artist_id_from_match("Primary", primary_first), ("UCprimary000", "ok"))
        self.assertEqual(match.artist_id_from_match("Featured", primary_first), (None, "artist_mismatch"))

    def test_pseudo_artists_never_get_a_photo(self):
        for name in ("Various Artists", "various artists", "Various", "Unknown Artist", "Unknown", "VA", "V.A.", "Сборник",
                     "Разные исполнители", "  VARIOUS   ARTISTS "):
            self.assertEqual(match.artist_id_from_match(name, candidate(name=name)), (None, "pseudo_artist"), name)
        # either side: a real track artist whose match is a compilation entry, and the other way round
        self.assertEqual(match.artist_id_from_match("The Killers", candidate(name="Various Artists"))[1], "pseudo_artist")
        self.assertEqual(match.artist_id_from_match("Various Artists", candidate())[1], "pseudo_artist")
        self.assertEqual(match.artist_id_from_match("Vanilla", candidate(name="Vanilla"))[1], "ok")  # not a substring rule

    def test_a_match_without_names_is_a_mismatch(self):
        m = candidate()
        m.artist_name, m.artists = None, ""
        self.assertEqual(match.artist_id_from_match("The Killers", m), (None, "artist_mismatch"))

    def test_photo_url_takes_the_base_and_the_square_suffix(self):
        page = {"thumbnails": [{"url": "https://yt3.googleusercontent.com/b=w540-h225-p-l90-rj"},
                               {"url": "https://yt3.googleusercontent.com/b=w2880-h1200-p-l90-rj"}]}
        self.assertEqual(match.artist_photo_url(page), "https://yt3.googleusercontent.com/b=w800-h800-p-l90-rj")

    def test_photo_url_none_for_missing_or_odd_pages(self):
        for page in (None, {}, [], {"thumbnails": []}, {"thumbnails": "x"}, {"thumbnails": [None, 3]},
                     {"thumbnails": [{"url": "http://x/y=w1"}]}, {"thumbnails": [{"url": "https://x/noequals"}]},
                     {"thumbnails": [{"url": 5}]}):
            self.assertIsNone(match.artist_photo_url(page))

    def test_photo_url_skips_a_bad_first_thumbnail(self):
        page = {"thumbnails": [{"url": "http://x/y=w1"}, {"url": "https://h/ok=w1"}]}
        self.assertEqual(match.artist_photo_url(page), "https://h/ok" + match.ARTIST_PHOTO_SUFFIX)


class GateTest(NoNetwork):
    """The startup column check, against the REAL schemas: a database before and after migration 0003."""

    def db(self, migrated):
        con = sqlite3.connect(":memory:")
        con.executescript((MIGRATIONS / "0001_init.sql").read_text())
        if migrated:
            con.executescript((MIGRATIONS / "0003_artist_images.sql").read_text())
        return con

    def query(self, con, log):
        def q(sql, params=None):
            log.append(sql)
            cur = con.execute(sql, params or [])
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return q

    def test_missing_column_switches_the_feature_off_without_a_failing_query(self):
        log = []
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(ingest.load_artist_images(self.query(self.db(False), log)))
        self.assertEqual(log, [ingest.ARTIST_COLUMN_SQL])  # the image_file_id SELECT never ran (it would fail)
        self.assertIn("pragma_table_info", ingest.ARTIST_COLUMN_SQL)
        self.assertEqual(out.getvalue().count("\n"), 1)  # one line
        self.assertIn("off", out.getvalue())

    def test_present_column_loads_the_stored_photos_only(self):
        con, log = self.db(True), []
        con.executemany("INSERT INTO artists (id, name, image_file_id) VALUES (?, ?, ?)",
                        [("a1", "A", "ph1"), ("a2", "B", None)])
        self.assertEqual(ingest.load_artist_images(self.query(con, log)), {"a1": "ph1"})
        self.assertEqual(log, [ingest.ARTIST_COLUMN_SQL, ingest.ARTIST_IMAGES_SQL])
        self.assertIn("image_file_id IS NOT NULL", ingest.ARTIST_IMAGES_SQL)

    def test_a_failing_check_switches_the_feature_off(self):
        for err in (ingest.IngestError("D1 API error 500"), OSError("x"), ValueError("not json"), KeyError("k"), TypeError("t")):
            def boom(sql, params=None):
                raise err
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertIsNone(ingest.load_artist_images(boom))
            self.assertIn("WARN", out.getvalue())
            self.assertNotIn("not json", out.getvalue())

    def test_a_non_json_answer_is_memoized_as_off(self):
        calls = []

        def bad():
            calls.append(1)
            return ingest.load_artist_images(lambda sql, params=None: (_ for _ in ()).throw(ValueError("x")))
        lazy = ingest.LazyArtistImages(bad)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual((lazy(), lazy()), (None, None))
        self.assertEqual(calls, [1])

    def test_default_query_is_d1_query_looked_up_at_call_time(self):
        with mock.patch.object(ingest, "d1_query", lambda sql, params=None: []), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(ingest.load_artist_images())

    def test_loader_runs_once_and_never_when_not_used(self):
        calls = []
        lazy = ingest.LazyArtistImages(lambda: calls.append(1) or {"x": "y"})
        self.assertEqual(calls, [])
        self.assertEqual((lazy(), lazy()), ({"x": "y"}, {"x": "y"}))
        self.assertEqual(calls, [1])
        off_calls = []
        off = ingest.LazyArtistImages(lambda: off_calls.append(1))  # None = off, remembered too
        self.assertEqual((off(), off()), (None, None))
        self.assertEqual(off_calls, [1])

    def test_end_to_end_missing_column_tracks_still_insert_and_never_name_the_column(self):
        con, log = self.db(False), []
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            h = Harness(d, loader=lambda: ingest.load_artist_images(self.query(con, log)))
            self.assertEqual(h.track(lfm(1)), "new")
            self.assertEqual(h.track(lfm(2)), "new")
        self.assertEqual(h.calls, [])
        for batch in h.fakes.batches:
            for sql, _ in batch:
                self.assertNotIn("image_file_id", sql)
                con.execute(sql, _)  # the real pre-migration schema accepts every statement

    def test_end_to_end_present_column_writes_the_photo_in_the_track_batch(self):
        con, log = self.db(True), []
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            h = Harness(d, loader=lambda: ingest.load_artist_images(self.query(con, log)))
            self.assertEqual(h.track(lfm(1)), "new")
        self.assertEqual([c[0] for c in h.calls], ["get_artist", "upload"])
        for sql, params in h.fakes.batches[0]:
            con.execute(sql, params)
        self.assertEqual(con.execute("SELECT image_file_id FROM artists").fetchall(), [("photo1",)])


class UpsertTest(NoNetwork):
    def test_without_an_image_the_artist_statement_is_the_old_one_byte_for_byte(self):
        sql, params = ingest.upsert_statements(SONG, 1, "f", 1, None)[0]
        self.assertEqual(sql, "INSERT INTO artists (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name")
        self.assertEqual(params, [ingest.artist_id("A"), "A"])
        self.assertEqual(ingest.upsert_statements(SONG, 1, "f", 1, "cov", None), ingest.upsert_statements(SONG, 1, "f", 1, "cov"))

    def test_with_an_image_the_write_is_coalesced_and_parameterized(self):
        stmts = ingest.upsert_statements(SONG, 1, "f", 1, None, "ph")
        sql, params = stmts[0]
        self.assertIn("COALESCE(artists.image_file_id, excluded.image_file_id)", sql)
        self.assertEqual(sql.count("?"), len(params))
        self.assertEqual(params, [ingest.artist_id("A"), "A", "ph"])
        self.assertNotIn("ph", sql)
        self.assertEqual(len(stmts), 3)
        self.assertEqual(stmts[1:], ingest.upsert_statements(SONG, 1, "f", 1, None)[1:])

    def test_the_sql_keeps_an_existing_photo_and_fills_a_missing_one(self):
        con = sqlite3.connect(":memory:")
        con.executescript((MIGRATIONS / "0001_init.sql").read_text() + (MIGRATIONS / "0003_artist_images.sql").read_text())
        for photo in ("PH1", "PH2"):
            for sql, params in ingest.upsert_statements(SONG, 1, "f", 1, None, photo):
                con.execute(sql, params)
        self.assertEqual(con.execute("SELECT image_file_id FROM artists").fetchall(), [("PH1",)])  # PH2 did not replace it
        con.execute("UPDATE artists SET image_file_id = NULL")
        for sql, params in ingest.upsert_statements(SONG, 1, "f", 1, None, "PH3"):
            con.execute(sql, params)
        self.assertEqual(con.execute("SELECT image_file_id FROM artists").fetchall(), [("PH3",)])

    def test_coalesce_keeps_a_stored_photo_when_no_new_one_is_given(self):
        con = sqlite3.connect(":memory:")
        con.executescript((MIGRATIONS / "0001_init.sql").read_text() + (MIGRATIONS / "0003_artist_images.sql").read_text())
        for sql, params in ingest.upsert_statements(SONG, 1, "f", 1, None, "first"):
            con.execute(sql, params)
        # a later track of the same artist without a photo (the old statement) and one with a NULL photo value
        con.execute("DELETE FROM tracks")
        other = dict(SONG, song_id="sp2")
        for sql, params in ingest.upsert_statements(other, 1, "f", 1, None):
            con.execute(sql, params)
        self.assertEqual(con.execute("SELECT image_file_id FROM artists").fetchall(), [("first",)])
        con.execute("INSERT INTO artists (id, name, image_file_id) VALUES (?, 'A', NULL) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                    "image_file_id = COALESCE(artists.image_file_id, excluded.image_file_id)", [ingest.artist_id("A")])
        self.assertEqual(con.execute("SELECT image_file_id FROM artists").fetchall(), [("first",)])


class ProcessTrackPhotoTest(NoNetwork):
    def test_a_new_artist_gets_one_call_and_the_photo_goes_into_the_track_batch(self):
        with tempfile.TemporaryDirectory() as d:
            h, notes = Harness(d), {}
            self.assertEqual(h.track(lfm(1), notes=notes), "new")
        self.assertEqual(h.calls, [("get_artist", "UCabcdefghij"), ("upload", PHOTO_URL)])
        sql, params = h.fakes.batches[0][0]
        self.assertIn("image_file_id", sql)
        self.assertEqual(params[-1], "photo1")
        self.assertTrue(notes["artist_photo"])
        self.assertEqual(h.images[ingest.artist_id("The Killers")], "photo1")

    def test_one_call_per_artist_per_run(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            for n in (1, 2, 3):
                self.assertEqual(h.track(lfm(n)), "new")
        self.assertEqual([c[0] for c in h.calls], ["get_artist", "upload"])
        # the later tracks carry the same value: the COALESCE write is idempotent
        self.assertEqual([b[0][1][-1] for b in h.fakes.batches], ["photo1"] * 3)

    def test_an_artist_with_a_stored_photo_gets_no_call_and_no_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, images={ingest.artist_id("The Killers"): "stored"})
            self.assertEqual(h.track(lfm(1)), "new")
        self.assertEqual(h.calls, [])
        self.assertEqual(h.fakes.batches[0][0][1][-1], "stored")

    def test_another_artist_is_looked_up_separately(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            h.track(lfm(1))
            h.track(lfm(2, artist="Muse"), m=candidate(artist_id="UCmuse000000", name="Muse"))
        self.assertEqual([c for c in h.calls if c[0] == "get_artist"], [("get_artist", "UCabcdefghij"), ("get_artist", "UCmuse000000")])

    def test_artist_guard_gates_only_the_photo_never_the_track(self):
        with tempfile.TemporaryDirectory() as d:
            h, notes = Harness(d), {}
            self.assertEqual(h.track(lfm(1), m=candidate(name="Cover Band"), notes=notes), "new")
        self.assertEqual(h.calls, [])
        self.assertNotIn("artist_photo", notes)
        self.assertNotIn("image_file_id", h.fakes.batches[0][0][0])
        self.assertIn("artist photo skipped", h.log[0])
        self.assertNotIn("Killers", h.log[0])
        self.assertNotIn("Cover Band", h.log[0])

    def test_a_guard_miss_is_not_remembered_so_a_better_match_still_gets_the_photo(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            h.track(lfm(1), m=candidate(name="Cover Band"))
            h.track(lfm(2), m=candidate())
        self.assertEqual([c[0] for c in h.calls], ["get_artist", "upload"])

    def test_no_artist_id_on_the_match_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            self.assertEqual(h.track(lfm(1), m=candidate(artist_id=None)), "new")
            self.assertEqual(h.track(lfm(2), m=object()), "new")
        self.assertEqual(h.calls, [])
        self.assertNotIn("artist photo", h.log[0])

    def test_featured_first_result_gives_no_call_no_photo_and_an_unchanged_statement(self):
        with tempfile.TemporaryDirectory() as d:
            h, notes = Harness(d), {}
            m = candidate(artist_id="UCfeatured00", name="Featured", joined="Featured, The Killers")
            self.assertEqual(h.track(lfm(1), m=m, notes=notes), "new")
        self.assertEqual(h.calls, [])
        self.assertNotIn("artist_photo", notes)
        self.assertEqual(h.fakes.batches[0][0], ingest.upsert_statements(lfm(1), 10, "f", 1, "cov")[0])
        self.assertIn("artist photo skipped", h.log[0])
        self.assertIn("artist_mismatch", h.log[0])
        self.assertNotIn("Featured", h.log[0])

    def test_primary_first_result_calls_with_the_primary_id(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            m = candidate(artist_id="UCprimary000", name="The Killers", joined="The Killers, Featured")
            self.assertEqual(h.track(lfm(1), m=m), "new")
        self.assertEqual(h.calls[0], ("get_artist", "UCprimary000"))

    def test_a_featured_first_miss_does_not_block_a_later_correct_match(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            h.track(lfm(1), m=candidate(artist_id="UCfeatured00", name="Featured", joined="Featured, The Killers"))
            h.track(lfm(2), m=candidate(artist_id="UCprimary000"))
        self.assertEqual([c for c in h.calls if c[0] == "get_artist"], [("get_artist", "UCprimary000")])

    def test_a_pseudo_artist_track_gets_no_call(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            self.assertEqual(h.track(lfm(1, artist="Various Artists"), m=candidate(name="Various Artists")), "new")
        self.assertEqual(h.calls, [])
        self.assertIn("pseudo_artist", h.log[0])

    def test_a_spotify_song_without_a_cover_url_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            s = dict(song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"), url="https://open.spotify.com/track/x")
            self.assertEqual(h.track(s), "new")
        self.assertEqual((h.calls, h.loads), ([], 0))

    def test_a_song_without_a_url_is_a_candidate_even_with_a_cover_url(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            s = dict(lfm(1), cover_url="https://i.scdn.co/image/x")
            self.assertEqual(h.track(s), "new")
        self.assertEqual([c[0] for c in h.calls], ["get_artist", "upload"])

    def test_spotify_sourced_songs_are_untouched_and_the_gate_is_not_even_read(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            s = song(1, cover_url="https://i.scdn.co/image/x")
            self.assertEqual(h.track(s), "new")
        self.assertEqual((h.calls, h.loads), ([], 0))
        self.assertEqual(h.fakes.batches[0][0], ingest.upsert_statements(s, 10, "f", 1, "cov")[0])

    def test_dry_run_makes_no_call_and_reads_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            self.assertEqual(h.track(lfm(1), dry=True), "dry_run")
        self.assertEqual((h.calls, h.loads, h.fakes.batches), ([], 0, []))

    def test_an_existing_track_makes_no_call(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            self.assertEqual(h.track(lfm(1), existing={lfm(1)["song_id"]}), "exists")
        self.assertEqual((h.calls, h.loads), ([], 0))

    def test_gate_off_means_no_call_and_the_old_statement(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, loader=lambda: None)
            self.assertEqual(h.track(lfm(1)), "new")
        self.assertEqual(h.calls, [])
        self.assertEqual(h.fakes.batches[0][0], ingest.upsert_statements(lfm(1), 10, "f", 1, "cov")[0])

    def test_without_the_deps_the_feature_is_off(self):
        for field in ("artist_images", "artist_photo_url", "upload_artist_image"):
            with tempfile.TemporaryDirectory() as d:
                h = Harness(d)
                setattr(h.deps, field, None)
                self.assertEqual(h.track(lfm(1)), "new")
            self.assertEqual(h.calls, [], field)

    def test_a_get_artist_failure_is_a_warning_and_is_not_retried_for_the_next_track(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, fail_get=RuntimeError("boom with Killers"))
            self.assertEqual(h.track(lfm(1)), "new")
            self.assertEqual(h.track(lfm(2)), "new")
        self.assertEqual(h.calls, [("get_artist", "UCabcdefghij")])
        self.assertIn("WARN artist photo failed", h.log[0])
        self.assertIn("RuntimeError", h.log[0])
        self.assertNotIn("Killers", h.log[0])  # class only, never the message or a name
        self.assertNotIn("artist photo", h.log[1])
        self.assertEqual(h.fakes.deleted, [])
        self.assertTrue(all("image_file_id" not in b[0][0] for b in h.fakes.batches))

    def test_an_upload_failure_never_fails_the_track(self):
        for err in (ingest.IngestError("cover host is not allowed"), OSError("x"), KeyError("photo"), ValueError("v")):
            with tempfile.TemporaryDirectory() as d:
                h = Harness(d, fail_upload=err)
                self.assertEqual(h.track(lfm(1)), "new")
            self.assertIn("WARN artist photo failed", h.log[0])
            self.assertNotIn("image_file_id", h.fakes.batches[0][0][0])

    def test_no_thumbnail_is_a_logged_skip(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, photo_url=None)
            self.assertEqual(h.track(lfm(1)), "new")
        self.assertEqual([c[0] for c in h.calls], ["get_artist"])
        self.assertIn("no_thumbnail", h.log[0])

    def test_the_sign_in_challenge_is_never_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, fail_get=ingest.YouTubeSignInRequired("bot check"))
            with self.assertRaises(ingest.YouTubeSignInRequired):
                h.track(lfm(1))
            self.assertEqual(h.fakes.batches, [])
            self.assertEqual(h.fakes.deleted, [1])  # the orphan audio message is removed as for any other failure

    def test_the_sign_in_challenge_in_run_ends_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d, fail_get=ingest.YouTubeSignInRequired("bot check"))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ingest.YouTubeSignInRequired) as cm:
                ingest.run([lfm(1), lfm(2)], h.deps, set(), {}, Path(d), False)
        self.assertEqual(cm.exception.tracks_not_attempted, 1)

    def test_counts_and_summary_name_no_artist(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(d)
            with contextlib.redirect_stdout(io.StringIO()):
                counts = ingest.run([lfm(1), lfm(2)], h.deps, set(), {}, Path(d), False)
        self.assertEqual(counts["artist_photo"], 1)
        self.assertIn("1 artist photos", ingest.summary(counts))
        base = {"new": 1, "exists": 0, "no_match": 0, "too_big": 0, "failed": 0}
        self.assertNotIn("artist photo", ingest.summary(base))  # additive, only when non-zero


class RealDepsTest(NoNetwork):
    class YTM:
        def __init__(self, page=None, exc=None):
            self.page, self.exc, self.asked = page, exc, []

        def get_artist(self, aid):
            self.asked.append(aid)
            if self.exc:
                raise self.exc
            return self.page

    def test_get_artist_result_becomes_the_square_crop_url(self):
        ytm = self.YTM({"thumbnails": [{"url": "https://yt3.googleusercontent.com/b=w540-h225-p-l90-rj"}]})
        deps = ingest.real_deps(ytm)
        self.assertEqual(deps.artist_photo_url("UCabcdefghij"), "https://yt3.googleusercontent.com/b" + match.ARTIST_PHOTO_SUFFIX)
        self.assertEqual(ytm.asked, ["UCabcdefghij"])

    def test_a_sign_in_text_from_get_artist_ends_the_run_as_a_challenge(self):
        deps = ingest.real_deps(self.YTM(exc=RuntimeError("Sign in to confirm you're not a bot")))
        with self.assertRaises(ingest.YouTubeSignInRequired):
            deps.artist_photo_url("UCabcdefghij")

    def test_other_get_artist_errors_propagate_unchanged(self):
        deps = ingest.real_deps(self.YTM(exc=RuntimeError("HTTP 500")))
        with self.assertRaises(RuntimeError):
            deps.artist_photo_url("UCabcdefghij")

    def test_the_photo_upload_uses_the_hardened_route(self):
        seen = []

        def fake_fetch(url, dest, allowed_hosts=None, *a, **k):
            seen.append((url, allowed_hosts))
            dest.write_bytes(JPEG)
            return dest
        with mock.patch.object(ingest, "fetch_cover", fake_fetch), \
                mock.patch.object(ingest, "tg_upload", lambda *a, **k: {"photo": [{"file_id": "big", "file_size": 9}]}), \
                tempfile.TemporaryDirectory() as d:
            self.assertEqual(ingest.real_deps(None).upload_artist_image(PHOTO_URL, Path(d)), "big")
        self.assertEqual(seen, [(PHOTO_URL, ingest.YTM_COVER_HOSTS)])  # https, exact hosts, no redirect, JPEG, 10 MB, deadline

    def test_a_hostile_photo_url_is_refused_by_the_real_fetch(self):
        deps = ingest.real_deps(None)
        with tempfile.TemporaryDirectory() as d:
            for url in ("http://yt3.googleusercontent.com/x", "https://evil.example/x.jpg",
                        "https://yt3.googleusercontent.com.evil.example/x", "https://user@yt3.googleusercontent.com/x"):
                with self.assertRaises(ingest.IngestError):
                    deps.upload_artist_image(url, Path(d))

    def test_the_default_gate_is_lazy_and_wired(self):
        deps = ingest.real_deps(None)
        self.assertIsInstance(deps.artist_images, ingest.LazyArtistImages)
        self.assertTrue(callable(deps.artist_photo_url) and callable(deps.upload_artist_image))

    def test_harden_deps_keeps_every_field(self):
        deps = ingest.real_deps(self.YTM({"thumbnails": []}), ingest.Throttle(0, 0, 3, sleep=lambda s: None))
        self.assertIsNotNone(deps.artist_images)
        self.assertIsNotNone(deps.upload_artist_image)
        self.assertEqual(deps.upload_cover.__name__, "cover")


class PacingTest(NoNetwork):
    def make(self, ytm, attempts=3):
        sleeps = []
        throttle = ingest.Throttle(5, 0, attempts, base=10, cap=120, sleep=sleeps.append)
        return ingest.real_deps(ytm, throttle), sleeps

    def test_each_get_artist_is_paced(self):
        ytm = RealDepsTest.YTM({"thumbnails": [{"url": "https://h/b=w1"}]})
        deps, sleeps = self.make(ytm)
        deps.artist_photo_url("UCabcdefghij")
        self.assertEqual(sleeps, [ingest.ARTIST_CALL_PAUSE_S])
        self.assertGreater(ingest.ARTIST_CALL_PAUSE_S, 0)

    def test_transient_errors_are_retried_a_bounded_number_of_times(self):
        ytm = RealDepsTest.YTM(exc=OSError("connection reset"))
        deps, sleeps = self.make(ytm, attempts=3)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            deps.artist_photo_url("UCabcdefghij")
        self.assertEqual(len(ytm.asked), 3)  # one try and two retries, then the error ends it

    def test_a_sign_in_challenge_is_not_retried_or_paced_around(self):
        ytm = RealDepsTest.YTM(exc=RuntimeError("Sign in to confirm you're not a bot"))
        deps, sleeps = self.make(ytm)
        with self.assertRaises(ingest.YouTubeSignInRequired):
            deps.artist_photo_url("UCabcdefghij")
        self.assertEqual(len(ytm.asked), 1)
        self.assertEqual(sleeps, [ingest.ARTIST_CALL_PAUSE_S])  # only the pause before the single call


if __name__ == "__main__":
    unittest.main()

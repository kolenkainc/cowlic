"""Tests for `bootstrap.py covers` and bootstrap_covers.py (#250). Offline: the D1 side is an in-memory SQLite database
built from the real migration (so the SQL itself is exercised), the matcher, the photo upload and the YouTube Music client
are fakes, every test has its own temp state directory (XDG_STATE_HOME), no .env is read, no network (NoNetwork base),
no real library data: every name below is invented."""
import contextlib
import datetime
import fcntl
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bootstrap
import bootstrap_covers as bc
import ingest
import match
import tg_check
from test_ingest import FixedToday
from test_lastfm_source import NoNetwork

MIGRATION = Path(__file__).resolve().parent.parent / "worker" / "migrations" / "0001_init.sql"
NOW_LOCAL = datetime.datetime(2026, 10, 11, 12, 0, 0)  # far from 21:10
BASE = "https://lh3.googleusercontent.com/FAKEBASE"
SENTINELS = ("Zorblax", "Quuxnaut", "Frobnitz", "Wibblewobble")  # names that must never reach an output or a state file


def cand(album="Quuxnaut Album", artist="Zorblax", base=BASE):
    return match.Candidate("vid1", "t", artist, 200, 1.0, album=album, artist_id="UCfake", artist_name=artist,
                           thumbnail_base=base)


class FakeD1:
    """The D1 database of the tests: SQLite with the real 0001 schema. query/write have the shape of the real ones."""

    def __init__(self, artists_image_column=False):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(MIGRATION.read_text())
        if artists_image_column:
            self.db.execute("ALTER TABLE artists ADD COLUMN image_file_id TEXT")
        self.queries: list = []
        self.writes: list = []
        self.write_hook = None
        self.n = 0

    def add(self, artist, album, title="Frobnitz", duration=200, cover=None, tracks=1):
        ar, al = ingest.artist_id(artist), ingest.album_id(artist, album)
        self.db.execute("INSERT OR IGNORE INTO artists (id, name) VALUES (?, ?)", [ar, artist])
        self.db.execute("INSERT OR IGNORE INTO albums (id, artist_id, name, cover_file_id) VALUES (?, ?, ?, ?)",
                        [al, ar, album, cover])
        for i in range(tracks):
            self.n += 1
            self.db.execute("INSERT INTO tracks (id, spotify_id, album_id, title, duration_s, suffix, content_type, "
                            "size_bytes, tg_file_id, tg_message_id) VALUES (?, ?, ?, ?, ?, 'mp3', 'audio/mpeg', 1, 'f', 1)",
                            [f"t{self.n:04d}", f"s{self.n}", al, title if i == 0 else f"{title} {i}", duration])
        return al

    def cover(self, album_id):
        return self.db.execute("SELECT cover_file_id FROM albums WHERE id = ?", [album_id]).fetchone()[0]

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        try:
            return [dict(r) for r in self.db.execute(sql, params or [])]
        except sqlite3.OperationalError as e:
            raise ingest.IngestError(f"D1 query failed: ['{e}']") from None

    def write(self, sql, params):
        self.writes.append((sql, params))
        if self.write_hook:
            self.write_hook(sql, params)
        cur = self.db.execute(sql, params)
        return [{"meta": {"changes": cur.rowcount}}]


class FakeBackends:
    """The matcher, the photo upload and the D1 write. `matches` maps an ALBUM NAME to what find_match returns
    (a Candidate, None, or an exception instance to raise); an album not listed gets a Candidate that passes the guard."""

    def __init__(self, d1, matches=None, upload_error=None):
        self.d1, self.matches, self.upload_error = d1, matches or {}, upload_error
        self.found: list = []
        self.uploads: list = []
        self.on_find = None

    def find(self, todo):
        self.found.append(todo)
        if self.on_find:
            self.on_find(todo)
        v = self.matches.get(todo.album, cand(album=todo.album, artist=todo.artist))
        if isinstance(v, Exception):
            raise v
        return v

    def upload(self, song, tdir):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append(song)
        return f"photo-{len(self.uploads)}"

    def backends(self):
        return bc.CoverBackends(self.find, self.upload, self.d1.write)


class Base(NoNetwork):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "state" / "cowlic"
        env = {"XDG_STATE_HOME": str(self.root / "state"), "XDG_CONFIG_HOME": str(self.root / "config"),
               **{k: "x" for k in bootstrap.COVERS_ENV}}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("COWLIC_LOCK_HELD", None)
        self.d1 = self.new_d1()
        self.fb = FakeBackends(self.d1)
        self.today = FixedToday("2026-10-11")
        self.constructed = 0
        for target, name, value in ((tg_check, "load_env", lambda: None),
                                    (bootstrap, "local_now", lambda: NOW_LOCAL),
                                    (bootstrap, "today", lambda: self.today()),
                                    (bootstrap, "real_cover_backends", self.make_backends),
                                    (bootstrap, "cover_query", lambda: self.d1.query)):
            q = mock.patch.object(target, name, value)
            q.start()
            self.addCleanup(q.stop)
        self.out = self.err = ""

    def new_d1(self, **kw):
        d1 = FakeD1(**kw)
        self.addCleanup(d1.db.close)
        return d1

    def make_backends(self):
        self.constructed += 1
        return self.fb.backends()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return bootstrap.main(list(argv))
        finally:
            self.out, self.err = out.getvalue(), err.getvalue()

    def covers(self, *extra, max_new="100", max_minutes="60"):
        return self.main("covers", "--max-new", max_new, "--max-minutes", max_minutes, *extra)

    def state(self) -> dict:
        return json.loads((self.dir / bc.STATE_FILE).read_text())

    def seed(self, n=3):
        """n cover-less albums; returns their ids in the order the run works them (ORDER BY id), see nm()."""
        ids = [self.d1.add("Zorblax", f"Album {i}", title=f"Song {i}") for i in range(n)]
        self.names = {ingest.album_id("Zorblax", f"Album {i}"): f"Album {i}" for i in range(n)}
        return sorted(ids)

    def nm(self, album_id):
        return self.names[album_id]


class AcceptedAndMismatchTest(Base):
    def test_an_accepted_match_stores_the_cover_with_the_parameterized_update(self):
        (al,) = self.seed(1)
        self.assertEqual(self.covers(), 0)
        self.assertEqual(self.d1.cover(al), "photo-1")
        self.assertEqual(self.d1.writes, [(bc.UPDATE_SQL, ["photo-1", al])])
        self.assertEqual(self.fb.uploads, [{"cover_url": BASE + match.COVER_SIZE_SUFFIX, ingest.COVER_FROM_MATCH: True}])
        self.assertIn("covered 1", self.out)
        self.assertNotIn("Album 0", self.out)

    def test_the_update_is_the_idempotent_statement(self):
        self.assertEqual(bc.UPDATE_SQL, "UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL")

    def test_one_stored_track_per_album_is_matched(self):
        self.d1.add("Zorblax", "Album 0", title="Song 0", tracks=4)
        self.covers()
        self.assertEqual([t.title for t in self.fb.found], ["Song 0"])  # the smallest track id of the album

    def test_the_stored_duration_is_handed_to_the_matcher_and_zero_means_title_only(self):
        self.d1.add("Zorblax", "A1", duration=0)
        self.d1.add("Zorblax", "A2", duration=187)
        self.covers()
        self.assertEqual({t.album: t.duration_s for t in self.fb.found}, {"A1": None, "A2": 187})

    def test_an_album_mismatch_is_skipped_counted_and_stays_null(self):
        (al,) = self.seed(1)
        self.fb.matches["Album 0"] = cand(album="Some Other Record")
        self.assertEqual(self.covers(), 0)
        self.assertIsNone(self.d1.cover(al))
        self.assertEqual(self.fb.uploads, [])
        self.assertEqual(self.d1.writes, [])
        self.assertIn("album_mismatch 1", self.out)
        self.assertEqual(self.state()["albums"][al]["result"], "mismatch")

    def test_the_edition_and_plural_rules_of_the_album_guard_apply(self):
        for stored, found in (("Album", "Album (Deluxe Edition)"), ("Lover", "Lovers"), ("Hits Vol. 1", "Hits Vol. 2")):
            with self.subTest(found=found):
                d1 = self.new_d1()
                al = d1.add("Zorblax", stored)
                fb = FakeBackends(d1, {stored: cand(album=found)})
                todo, _ = bc.read_todo(d1.query)
                run = bc.run_backfill(todo, fb.backends(), bc.CoverState(self.dir / "s.json"), ingest.RunLimits(5, 5),
                                      self.today, 30)
                self.assertEqual(run.counts["album_mismatch"], 1)
                self.assertIsNone(d1.cover(al))

    def test_an_artist_mismatch_is_skipped_and_the_cyrillic_variant_is_accepted(self):
        (al,) = self.seed(1)
        self.fb.matches["Album 0"] = cand(album="Album 0", artist="Somebody Else Entirely")
        self.covers()
        self.assertIsNone(self.d1.cover(al))
        self.assertIn("artist_mismatch 1", self.out)
        d1 = self.new_d1()
        al2 = d1.add("Zorblax", "Quuxnaut Album")
        fb = FakeBackends(d1, {"Quuxnaut Album": cand(album="Quuxnaut Album", artist="Зорблакс")})  # an invented name
        todo, _ = bc.read_todo(d1.query)
        run = bc.run_backfill(todo, fb.backends(), bc.CoverState(self.dir / "s2.json"), ingest.RunLimits(5, 5), self.today, 30)
        self.assertEqual(run.counts["covered"], 1)
        self.assertEqual(d1.cover(al2), "photo-1")

    def test_no_match_and_a_match_without_thumbnail_leave_the_album_null(self):
        a, b = self.seed(2)
        self.fb.matches[self.nm(a)] = None
        self.fb.matches[self.nm(b)] = cand(album=self.nm(b), base=None)
        self.covers()
        self.assertEqual((self.d1.cover(a), self.d1.cover(b)), (None, None))
        self.assertIn("no_match 1", self.out)
        self.assertIn("no_cover_in_match 1", self.out)
        self.assertEqual({e["result"] for e in self.state()["albums"].values()}, {"no_match", "mismatch"})

    def test_the_unknown_album_is_never_filled_and_albums_with_a_cover_are_not_selected(self):
        self.d1.add("Zorblax", ingest.UNKNOWN_ALBUM)
        self.d1.add("Zorblax", "unknown album")
        done = self.d1.add("Zorblax", "Done Album", cover="old")
        self.covers()
        self.assertEqual(self.fb.found, [])
        self.assertEqual(self.d1.cover(done), "old")
        self.assertEqual(self.d1.writes, [])

    def test_an_album_without_tracks_is_not_selected(self):
        ar = ingest.artist_id("Zorblax")
        self.d1.db.execute("INSERT INTO artists (id, name) VALUES (?, 'Zorblax')", [ar])
        self.d1.db.execute("INSERT INTO albums (id, artist_id, name) VALUES (?, ?, 'Empty')", [ingest.album_id("Zorblax", "Empty"), ar])
        self.covers()
        self.assertEqual(self.fb.found, [])

    def test_a_stopped_run_resumes_by_itself_from_d1(self):
        ids = self.seed(5)
        self.assertEqual(self.covers(max_new="2"), 0)
        self.assertEqual([self.d1.cover(a) is not None for a in ids], [True, True, False, False, False])
        self.fb.found.clear()
        self.assertEqual(self.covers(max_new="10"), 0)
        self.assertEqual([t.album for t in self.fb.found], [self.nm(a) for a in ids[2:]])
        self.assertTrue(all(self.d1.cover(a) for a in ids))
        self.fb.found.clear()
        self.covers()
        self.assertEqual(self.fb.found, [])  # nothing left: no work, no search


class CapsTest(Base):
    def test_max_new_counts_albums_attempted_and_stops(self):
        ids = self.seed(5)
        self.fb.matches.update({self.nm(ids[0]): None, self.nm(ids[1]): cand(album="x y z")})  # misses count as attempts too
        self.assertEqual(self.covers(max_new="3"), 0)
        self.assertEqual(len(self.fb.found), 3)
        self.assertIn("stop reason: cap", self.out)
        self.assertIn("not attempted 2", self.out)

    def test_max_minutes_stops_before_the_next_album(self):
        self.seed(5)
        clock = [0.0]
        self.fb.on_find = lambda t: clock.__setitem__(0, clock[0] + 100)  # every album takes 100 s
        with mock.patch.object(bootstrap, "make_limits", lambda n, m: ingest.RunLimits(n, m, clock=lambda: clock[0])):
            self.assertEqual(self.covers(max_minutes="3"), 0)
        self.assertEqual(len(self.fb.found), 2)  # 0 s, 100 s start a new album; 200 s >= 180 s does not
        self.assertIn("stop reason: time", self.out)

    def test_both_caps_are_required_and_range_checked(self):
        self.seed(1)
        for argv in (("covers",), ("covers", "--max-new", "5"), ("covers", "--max-minutes", "5"),
                     ("covers", "--max-new", "0", "--max-minutes", "5"), ("covers", "--max-new", "5", "--max-minutes", "1441"),
                     ("covers", "--max-new", "x", "--max-minutes", "5")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv), 2)
        self.assertEqual((self.fb.found, self.constructed), ([], 0))

    def test_the_bounds_are_accepted(self):
        self.assertEqual(self.covers(max_new="1000000", max_minutes="1"), 0)

    def test_abbreviations_are_not_accepted(self):
        self.assertEqual(self.main("covers", "--max-n", "5", "--max-minutes", "5"), 2)
        self.assertEqual(self.main("covers", "--cou"), 2)

    def test_a_clean_done_run_says_done(self):
        self.seed(2)
        self.covers()
        self.assertIn("stop reason: done", self.out)


class LockAndGuardTest(Base):
    def hold(self):
        self.dir.mkdir(parents=True, mode=0o700)
        fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def test_a_held_lock_exits_75_and_does_nothing(self):
        self.seed(2)
        self.hold()
        self.assertEqual(self.covers(), 75)
        self.assertEqual((self.fb.found, self.d1.writes, self.d1.queries, self.constructed), ([], [], [], 0))
        self.assertFalse((self.dir / bc.STATE_FILE).exists())

    def test_the_count_takes_the_lock_too(self):
        self.hold()
        self.assertEqual(self.main("covers", "--count"), 75)
        self.assertEqual(self.d1.queries, [])

    def test_the_lock_is_released_after_the_run(self):
        self.seed(1)
        self.covers()
        fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise BlockingIOError when still held

    def test_through_the_wrapper_the_lock_is_not_taken_again(self):
        self.seed(1)
        self.hold()
        with mock.patch.dict(os.environ, {"COWLIC_LOCK_HELD": "1"}):
            self.assertEqual(self.covers(), 0)

    def test_the_guard_refuses_a_session_that_reaches_21_10(self):
        self.seed(2)
        for now, minutes, code in ((datetime.datetime(2026, 10, 11, 20, 30), "30", 78),
                                   (datetime.datetime(2026, 10, 11, 12, 0), "538", 0),
                                   (datetime.datetime(2026, 10, 11, 12, 0), "539", 78),
                                   (datetime.datetime(2026, 10, 11, 21, 10, 30), "1", 78)):
            with self.subTest(now=now, minutes=minutes), mock.patch.object(bootstrap, "local_now", lambda n=now: n):
                before = (len(self.fb.found), self.constructed, len(self.d1.queries))
                self.assertEqual(self.covers(max_minutes=minutes), code)
                if code == 78:  # refused before D1, the matcher or Telegram were touched
                    self.assertEqual((len(self.fb.found), self.constructed, len(self.d1.queries)), before)
                    self.assertIn("next 21:10", self.err)

    def test_the_guard_is_checked_after_the_lock(self):
        self.hold()
        with mock.patch.object(bootstrap, "local_now", lambda: datetime.datetime(2026, 10, 11, 21, 5)):
            self.assertEqual(self.covers(), 75)

    def test_the_count_has_no_guard(self):
        self.seed(1)
        with mock.patch.object(bootstrap, "local_now", lambda: datetime.datetime(2026, 10, 11, 21, 9, 30)):
            self.assertEqual(self.main("covers", "--count"), 0)


class SignInTest(Base):
    def test_a_sign_in_challenge_stops_at_once_with_exit_3_and_is_never_retried(self):
        a, b, c = self.seed(3)
        self.fb.matches[self.nm(b)] = ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
        result = self.root / "r.json"
        self.assertEqual(self.covers("--result-json", str(result)), 3)
        self.assertEqual([t.album for t in self.fb.found], [self.nm(a), self.nm(b)])  # the third is never attempted
        self.assertEqual((self.d1.cover(a), self.d1.cover(b), self.d1.cover(c)), ("photo-1", None, None))
        self.assertIn("youtube-sign-in", self.out)
        doc = json.loads(result.read_text())
        self.assertEqual((doc["stop_reason"], doc["exit_code"]), ("sign-in", 3))
        self.assertEqual(doc["counts"]["not_attempted"], 2)
        self.assertFalse((self.dir / bc.STATE_FILE).exists())  # a sign-in stop is not a cooldown case

    def test_the_search_wrapper_turns_the_bot_check_text_into_the_stop_and_does_not_retry(self):
        class Ytm:
            calls = 0

            def search(self, q, filter=None, limit=None):
                Ytm.calls += 1
                raise RuntimeError("Sign in to confirm you’re not a bot")
        slept = []
        paced = bc.PacedSearch(Ytm(), ingest.Throttle(0, 0, 3, sleep=slept.append), sleep=slept.append)
        with self.assertRaises(ingest.YouTubeSignInRequired):
            paced.search("q", filter="songs", limit=10)
        self.assertEqual((Ytm.calls, slept), (1, []))



class PacingTest(NoNetwork):
    class Ytm:
        def __init__(self, errors=()):
            self.errors, self.calls = list(errors), 0

        def search(self, q, filter=None, limit=None):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            return []

    def paced(self, ytm, rng=lambda: 0.5, attempts=3):
        self.t = 0.0
        self.sleeps: list = []

        def sleep(s):
            self.sleeps.append(s)
            self.t += s
        return bc.PacedSearch(ytm, ingest.Throttle(0, 0, attempts, sleep=sleep, clock=lambda: self.t),
                              sleep=sleep, clock=lambda: self.t, rng=rng)

    def test_at_least_1_5_seconds_between_calls_plus_jitter(self):
        p = self.paced(self.Ytm())
        for _ in range(4):
            p.search("q")
        self.assertEqual(len(self.sleeps), 3)  # none before the first call
        self.assertTrue(all(abs(s - 2.0) < 1e-9 for s in self.sleeps))  # 1.5 + 0.5 * 1.0 with an instant call
        self.assertGreaterEqual(bc.PACE_SECONDS, 1.5)

    def test_time_spent_in_the_call_counts_toward_the_gap(self):
        ytm = self.Ytm()
        p = self.paced(ytm, rng=lambda: 0.0)
        p.search("q")
        self.t += 1.0  # one second elapsed since the call ended
        p.search("q")
        self.assertEqual(self.sleeps, [0.5])

    def test_jitter_is_bounded(self):
        for r in (0.0, 0.999):
            p = self.paced(self.Ytm(), rng=lambda r=r: r)
            p.search("q")
            p.search("q")
            self.assertTrue(1.5 <= self.sleeps[0] <= 2.5)

    def test_transient_errors_are_retried_a_bounded_number_of_times(self):
        ytm = self.Ytm([OSError("conn reset"), RuntimeError("HTTP Error 503: x")])
        self.paced(ytm).search("q")
        self.assertEqual(ytm.calls, 3)
        ytm = self.Ytm([OSError("x")] * 5)
        with self.assertRaises(OSError):
            self.paced(ytm).search("q")
        self.assertEqual(ytm.calls, 3)  # attempts, not endless

    def test_other_errors_are_not_retried(self):
        ytm = self.Ytm([ValueError("bad json")])
        with self.assertRaises(ValueError):
            self.paced(ytm).search("q")
        self.assertEqual(ytm.calls, 1)

    def test_every_search_of_a_multi_query_match_is_paced(self):
        ytm = self.Ytm()
        p = self.paced(ytm)
        self.assertIsNone(match.find_match(p, "Song", "Zorblax", "Album", 200))  # 2 queries (plus a Cyrillic one for Latin)
        self.assertEqual(ytm.calls, 3)
        self.assertEqual(len(self.sleeps), 2)


class CooldownTest(Base):
    def test_a_hopeless_album_rests_and_is_not_retried_or_counted(self):
        a, b = self.seed(2)
        self.fb.matches[self.nm(a)] = None
        self.covers()
        self.assertEqual(self.state()["albums"][a], {"result": "no_match", "on": "2026-10-11"})
        self.fb.found.clear()
        self.covers(max_new="1")
        self.assertEqual(self.fb.found, [])  # a is resting, b is stored: nothing to attempt
        self.assertIn("cooldown 1", self.out)

    def test_a_cooled_album_does_not_use_up_the_cap(self):
        ids = self.seed(4)
        for a in ids[:3]:
            self.fb.matches[self.nm(a)] = None
        self.covers(max_new="3")  # the first three rest now
        self.assertEqual(len(self.state()["albums"]), 3)
        self.fb.found.clear()
        self.covers(max_new="1")
        self.assertEqual([t.album for t in self.fb.found], [self.nm(ids[3])])  # attempted although 3 rest before it
        self.assertIn("cooldown 3", self.out)

    def test_the_album_is_retried_after_the_cooldown(self):
        (a,) = self.seed(1)
        self.fb.matches["Album 0"] = None
        self.covers()
        self.today = FixedToday("2026-11-09")  # day 29
        self.fb.found.clear()
        self.covers()
        self.assertEqual(self.fb.found, [])
        self.today = FixedToday("2026-11-10")  # day 30
        self.covers()
        self.assertEqual(len(self.fb.found), 1)

    def test_retry_days_0_always_retries_and_a_larger_value_waits_longer(self):
        (a,) = self.seed(1)
        self.fb.matches["Album 0"] = None
        self.covers()
        self.fb.found.clear()
        self.covers("--retry-days", "0")
        self.assertEqual(len(self.fb.found), 1)
        self.today = FixedToday("2026-12-31")
        self.fb.found.clear()
        self.covers("--retry-days", "365")
        self.assertEqual(self.fb.found, [])

    def test_a_later_success_removes_the_entry(self):
        (a,) = self.seed(1)
        self.fb.matches["Album 0"] = None
        self.covers()
        del self.fb.matches["Album 0"]
        self.covers("--retry-days", "0")
        self.assertEqual(self.state()["albums"], {})
        self.assertEqual(self.d1.cover(a), "photo-1")

    def test_failures_are_not_cooled(self):
        (a,) = self.seed(1)
        self.fb.upload_error = ingest.IngestError("cover is not image/jpeg")
        self.assertEqual(self.covers(), 1)
        self.assertFalse((self.dir / bc.STATE_FILE).exists())

    def test_the_file_is_private_atomic_and_holds_ids_and_words_only(self):
        a, b = self.seed(2)
        self.fb.matches[self.nm(a)] = None
        self.fb.matches[self.nm(b)] = cand(album="Frobnitz Wibblewobble")
        self.covers()
        path = self.dir / bc.STATE_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        text = path.read_text()
        for s in SENTINELS + ("Album 0", "Song 0"):
            self.assertNotIn(s, text)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".")], [])  # no temp file left
        self.assertEqual(set(json.loads(text)), {"version", "albums"})

    def test_a_failed_state_write_is_logged_exit_1_and_the_run_goes_on(self):
        a, b = self.seed(2)
        self.fb.matches[self.nm(a)] = None
        self.fb.matches[self.nm(b)] = None
        with mock.patch.object(ingest, "atomic_write_json", side_effect=OSError("disk")):
            self.assertEqual(self.covers(), 1)
        self.assertEqual(len(self.fb.found), 2)
        self.assertIn("could not write", self.out)

    def test_malformed_files_are_refused_with_exit_2_and_nothing_is_done(self):
        self.seed(1)
        self.dir.mkdir(parents=True, mode=0o700)
        path = self.dir / bc.STATE_FILE
        good = ingest.album_id("a", "b")
        for body in ("not json", "[]", '{"version": 1}', '{"version": 2, "albums": {}}',
                     '{"version": 1, "albums": {}, "x": 1}', '{"version": 1, "albums": {"zz": {"result": "no_match", "on": "2026-10-11"}}}',
                     json.dumps({"version": 1, "albums": {good: {"result": "failed", "on": "2026-10-11"}}}),
                     json.dumps({"version": 1, "albums": {good: {"result": "no_match", "on": "2026-13-01"}}}),
                     json.dumps({"version": 1, "albums": {good: {"result": "no_match", "on": "2026-10-11", "x": 1}}}),
                     '{"version": 1, "albums": {}, "n": NaN}'):
            with self.subTest(body=body[:40]):
                path.write_text(body)
                self.assertEqual(self.covers(), 2)
                self.assertEqual(self.main("covers", "--count"), 2)
                self.assertIn("state file unusable", self.err)
        self.assertEqual((self.fb.found, self.d1.writes, self.constructed), ([], [], 0))
        path.unlink()
        path.mkdir()
        self.assertEqual(self.covers(), 2)

    def test_a_valid_file_is_accepted(self):
        self.seed(1)
        self.dir.mkdir(parents=True, mode=0o700)
        (self.dir / bc.STATE_FILE).write_text(json.dumps({"version": 1, "albums": {ingest.album_id("a", "b"): {"result": "mismatch", "on": "2026-10-01"}}}))
        self.assertEqual(self.covers(), 0)


class IdempotenceTest(Base):
    def test_a_cover_stored_meanwhile_is_never_replaced(self):
        (a,) = self.seed(1)

        def other_process(sql, params):
            self.d1.db.execute("UPDATE albums SET cover_file_id = 'first-one' WHERE id = ?", [a])
        self.d1.write_hook = other_process
        self.assertEqual(self.covers(), 0)
        self.assertEqual(self.d1.cover(a), "first-one")
        self.assertIn("already 1", self.out)
        self.assertIn("covered 0", self.out)

    def test_a_second_run_over_stored_albums_writes_nothing(self):
        self.seed(2)
        self.covers()
        n = len(self.d1.writes)
        self.covers()
        self.assertEqual(len(self.d1.writes), n)

    def test_the_statement_never_overwrites_on_the_real_sqlite_semantics(self):
        a = self.d1.add("Zorblax", "Keep", cover="keep-me")
        cur = self.d1.db.execute(bc.UPDATE_SQL, ["new", a])
        self.assertEqual((cur.rowcount, self.d1.cover(a)), (0, "keep-me"))

    def test_statements_are_parameterized_even_for_hostile_values(self):
        evil = "x'); DROP TABLE albums; --"
        a = self.d1.add("Zorblax", evil)
        self.fb.matches[evil] = cand(album=evil)
        self.fb.upload = lambda song, tdir: evil  # a hostile file id must stay a value
        self.covers()
        self.assertEqual(self.d1.cover(a), evil)
        for sql, params in self.d1.queries + self.d1.writes:
            self.assertNotIn(evil, sql)
        self.assertTrue(self.d1.db.execute("SELECT count(*) FROM albums").fetchone()[0] >= 1)


class FailureTest(Base):
    def test_a_failed_upload_leaves_the_album_null_exit_1_and_the_run_goes_on(self):
        a, b = self.seed(2)
        calls = []

        def upload(song, tdir):
            calls.append(1)
            if len(calls) == 1:
                raise ingest.IngestError("Telegram API error 429 secret-token-text")
            return "photo-2"
        self.fb.upload = upload
        self.assertEqual(self.covers(), 1)
        self.assertEqual((self.d1.cover(a), self.d1.cover(b)), (None, "photo-2"))
        self.assertIn("failed 1", self.out)
        self.assertNotIn("secret-token-text", self.out + self.err)  # the message text of an upstream error is never logged
        self.assertIn("IngestError", self.out)

    def test_a_d1_write_failure_is_a_failure_not_a_cover(self):
        a, = self.seed(1)

        def boom(sql, params):
            raise ingest.IngestError("D1 API error 500")
        self.d1.write_hook = boom
        self.assertEqual(self.covers(), 1)
        self.assertIsNone(self.d1.cover(a))

    def test_three_failures_in_a_row_stop_the_run(self):
        self.seed(6)
        self.fb.upload_error = ingest.IngestError("down")
        self.assertEqual(self.covers(), 1)
        self.assertEqual(len(self.fb.found), bc.MAX_CONSECUTIVE_FAILURES)
        self.assertIn("stop reason: failed", self.out)

    def test_a_success_resets_the_failure_streak(self):
        self.seed(6)
        seq = iter([1, 1, 0, 1, 1, 0])

        def upload(song, tdir):
            if next(seq):
                raise ingest.IngestError("x")
            return "ok"
        self.fb.upload = upload
        self.assertEqual(self.covers(), 1)
        self.assertEqual(len(self.fb.found), 6)

    def test_a_d1_read_error_is_exit_1_and_nothing_else_runs(self):
        self.d1.query = lambda sql, params=None: (_ for _ in ()).throw(ingest.IngestError("D1 API error 500"))
        self.assertEqual(self.covers(), 1)
        self.assertEqual(self.constructed, 0)

    def test_the_search_error_of_one_album_is_a_failure_of_that_album(self):
        a, b = self.seed(2)
        self.fb.matches[self.nm(a)] = OSError("network")
        self.assertEqual(self.covers(), 1)
        self.assertEqual((self.d1.cover(a), self.d1.cover(b)), (None, "photo-1"))


class WiringTest(Base):
    def test_the_real_upload_path_gets_the_host_allowlist_and_the_photo_route(self):
        """attempt() hands ingest.real_deps(...).upload_cover a song the #248 route recognizes: fetch_cover is called with
        the YouTube host allowlist and the file id is the largest sendPhoto size."""
        self.seed(1)
        deps = ingest.real_deps(None)
        fetched = []

        def fake_fetch(url, dest, allowed_hosts=None, **kw):
            fetched.append((url, allowed_hosts))
            dest.write_bytes(b"\xff\xd8x")
            return dest
        sent = []

        def fake_tg(method, field, path, ctype, extra=None):
            sent.append((method, field))
            return {"photo": [{"file_id": "small", "file_size": 1, "width": 1, "height": 1},
                              {"file_id": "big", "file_size": 9, "width": 9, "height": 9}]}
        self.fb.upload = deps.upload_cover
        with mock.patch.object(ingest, "fetch_cover", fake_fetch), mock.patch.object(ingest, "tg_upload", fake_tg):
            self.assertEqual(self.covers(), 0)
        self.assertEqual(fetched, [(BASE + match.COVER_SIZE_SUFFIX, ingest.YTM_COVER_HOSTS)])
        self.assertEqual(sent, [("sendPhoto", "photo")])
        self.assertEqual(self.d1.writes[0][1][0], "big")

    def test_a_hostile_cover_response_is_a_failure_and_stores_nothing(self):
        (a,) = self.seed(1)
        self.fb.upload = ingest.real_deps(None).upload_cover

        def hostile(url, dest, allowed_hosts=None, **kw):
            raise ingest.IngestError("cover host is not allowed")
        with mock.patch.object(ingest, "fetch_cover", hostile):
            self.assertEqual(self.covers(), 1)
        self.assertEqual((self.d1.cover(a), self.d1.writes), (None, []))

    def test_the_real_backends_pace_the_search_and_use_the_shared_matcher(self):
        import sys
        import types
        calls = []

        class Ytm:
            def search(self, q, filter=None, limit=None):
                calls.append((q, filter, limit))
                return []
        fake_mod = types.SimpleNamespace(YTMusic=Ytm)
        naps = []
        with mock.patch.dict(sys.modules, {"ytmusicapi": fake_mod}), mock.patch.object(bc.time, "sleep", naps.append):
            b = bc.real_backends()
            self.assertIsNone(b.find_match(bc.Todo("0" * 16, "Album", "Zorblax", "Song", 200)))
        self.assertEqual(calls[0], ("Song Album", "songs", 10))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(naps), 2)  # paced between the three searches, not before the first
        self.assertTrue(all(1.0 < n <= 2.5 for n in naps))  # the real pace: 1.5 s + jitter, less the time already spent


class CountTest(Base):
    def test_the_count_is_read_only_offline_and_prints_counts(self):
        self.seed(3)
        self.d1.add("Zorblax", ingest.UNKNOWN_ALBUM)
        self.d1.add("Zorblax", "Done", cover="c")
        self.fb.upload_error = AssertionError("the count must not upload")
        self.d1.write_hook = lambda s, p: (_ for _ in ()).throw(AssertionError("the count must not write"))
        before = self.d1.db.execute("SELECT count(*) FROM albums WHERE cover_file_id IS NULL").fetchone()[0]
        self.assertEqual(self.main("covers", "--count"), 0)
        self.assertEqual(self.constructed, 0)  # no matcher, no Telegram client was even built
        self.assertEqual((self.fb.found, self.d1.writes), ([], []))
        self.assertIn("albums without a cover: 4 (to try: 3, in cooldown: 0, unknown album: 1", self.out)
        self.assertIn("artists without an image: not counted yet", self.out)
        self.assertEqual(self.d1.db.execute("SELECT count(*) FROM albums WHERE cover_file_id IS NULL").fetchone()[0], before)
        self.assertFalse((self.dir / bc.STATE_FILE).exists())
        self.assertEqual([p.name for p in self.dir.iterdir()], [bootstrap.LOCK_FILE])  # only the lock file
        for _sql, _params in self.d1.queries:
            self.assertTrue(_sql.lstrip().upper().startswith("SELECT"))

    def test_the_count_includes_the_cooldown_and_artists_with_the_column(self):
        self.d1 = self.new_d1(artists_image_column=True)
        self.fb = FakeBackends(self.d1)
        a = self.d1.add("Zorblax", "Rest")
        self.d1.add("Zorblax", "Try")
        self.d1.add("Other Band", "Try 2")
        self.dir.mkdir(parents=True, mode=0o700)
        (self.dir / bc.STATE_FILE).write_text(json.dumps({"version": 1, "albums": {
            a: {"result": "no_match", "on": "2026-10-01"},
            ingest.album_id("gone", "gone"): {"result": "no_match", "on": "2026-10-01"}}}))
        self.assertEqual(self.main("covers", "--count"), 0)
        self.assertIn("albums without a cover: 3 (to try: 2, in cooldown: 1,", self.out)
        self.assertIn("artists without an image: 2 (in cooldown: 0)", self.out)
        self.assertEqual(self.main("covers", "--count", "--retry-days", "0"), 0)
        self.assertIn("to try: 3, in cooldown: 0", self.out)

    def test_the_count_takes_no_caps_and_no_result_file(self):
        for extra in (("--max-new", "5"), ("--max-minutes", "5"), ("--result-json", str(self.root / "r.json"))):
            with self.subTest(extra=extra):
                self.assertEqual(self.main("covers", "--count", *extra), 2)
        self.assertEqual(self.d1.queries, [])

    def test_the_count_needs_only_the_d1_variables(self):
        self.seed(1)
        for k in ("TG_BOT_TOKEN", "TG_CHANNEL_ID"):
            os.environ.pop(k)
        self.assertEqual(self.main("covers", "--count"), 0)
        self.assertEqual(self.covers(), 2)  # a session needs Telegram
        self.assertIn("missing env vars: TG_BOT_TOKEN, TG_CHANNEL_ID", self.err)

    def test_a_d1_error_other_than_a_missing_column_is_a_failure(self):
        real = self.d1.query

        def q(sql, params=None):
            if sql == bc.ARTISTS_COUNT_SQL:
                raise ingest.IngestError("D1 API error 500")
            return real(sql, params)
        self.d1.query = q
        self.assertEqual(self.main("covers", "--count"), 1)

    def test_no_env_syntax_error_and_missing_d1_variables_are_exit_2(self):
        os.environ.pop("D1_DATABASE_ID")
        self.assertEqual(self.main("covers", "--count"), 2)
        self.assertIn("missing env vars: D1_DATABASE_ID", self.err)


class IsolationTest(Base):
    def test_the_ranking_and_the_progress_file_are_never_read_or_written(self):
        self.seed(2)
        self.dir.mkdir(parents=True, mode=0o700)
        files = {name: b"\x00garbage, not json" for name in ("bootstrap-ranking.json", "bootstrap-progress.json",
                                                              "bootstrap-state.json", "ingest-state.json")}
        for name, body in files.items():
            (self.dir / name).write_bytes(body)
        self.assertEqual(self.covers(), 0)
        self.assertEqual(self.main("covers", "--count"), 0)
        for name, body in files.items():
            self.assertEqual((self.dir / name).read_bytes(), body)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([*files, bootstrap.LOCK_FILE]))  # nothing new

    def test_no_config_and_no_lastfm_key_are_needed(self):
        self.seed(1)
        os.environ.pop("LASTFM_API_KEY", None)
        self.assertEqual(self.covers(), 0)
        self.assertEqual(self.main("covers", "--config", "x", "--max-new", "1", "--max-minutes", "1"), 2)  # no such option

    def test_logs_and_results_hold_counts_and_class_words_only(self):
        self.d1.add("Zorblax", "Quuxnaut Album", title="Frobnitz")
        self.d1.add("Zorblax", "Wibblewobble", title="Frobnitz 2")
        self.fb.matches["Wibblewobble"] = cand(album="Frobnitz Other")
        result = self.root / "r.json"
        self.covers("--result-json", str(result))
        everything = self.out + self.err + result.read_text() + (self.dir / bc.STATE_FILE).read_text()
        for s in SENTINELS:
            self.assertNotIn(s, everything)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        doc = json.loads(result.read_text())
        self.assertEqual(set(doc), {"stop_reason", "exit_code", "counts", "artists"})
        self.assertEqual((doc["counts"]["covered"], doc["counts"]["album_mismatch"]), (1, 1))

    def test_help_lists_the_subcommand(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.main("covers", "--help"), 0)
        self.assertEqual(bootstrap.build_parser().parse_args(["covers", "--count"]).count, True)


class ReadTodoTest(NoNetwork):
    def test_rows_are_validated_not_trusted(self):
        good = ingest.album_id("a", "b")
        rows = [{"album_id": good, "album": "A", "artist": "B", "title": "T", "duration_s": 5},
                {"album_id": "not-an-id", "album": "A", "artist": "B", "title": "T", "duration_s": 5},
                {"album_id": good, "album": None, "artist": "B", "title": "T", "duration_s": 5},
                {"album_id": good, "album": "  ", "artist": "B", "title": "T", "duration_s": 5},
                {"album_id": good, "album": "Unknown Album", "artist": "B", "title": "T", "duration_s": 5},
                {"album_id": good, "album": "A", "artist": "B", "title": "T", "duration_s": True},
                {"album_id": good, "album": "A", "artist": "B", "title": "T", "duration_s": -3},
                "junk"]
        todo, skipped = bc.read_todo(lambda sql, params=None: rows)
        self.assertEqual([(t.duration_s) for t in todo], [5, None, None])
        self.assertEqual(skipped, {"unknown": 1, "bad": 4})

    def test_the_query_is_parameterless_and_read_only(self):
        seen = []
        bc.read_todo(lambda sql, params=None: seen.append((sql, params)) or [])
        self.assertEqual(seen, [(bc.ALBUMS_SQL, None)])
        self.assertTrue(bc.ALBUMS_SQL.startswith("SELECT"))
        self.assertIn("cover_file_id IS NULL", bc.ALBUMS_SQL)


if __name__ == "__main__":
    unittest.main()

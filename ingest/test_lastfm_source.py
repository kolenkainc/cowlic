"""Tests for the lastfm_artists source (#152). Everything is faked: no Last.fm, no YouTube, no Spotify, no network."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ingest
import lastfm
import lastfm_tracks
import match
import spotify_user
from test_followed import aid, art
from test_ingest import GOOD_URL, FixedToday, Fakes, sid, song

REPO = Path(__file__).resolve().parent.parent
REAL_SPOTDL_RUN = ingest.spotdl_run  # the guard in NoNetwork replaces the module attribute
KEY = "k" * 32  # a well-formed fake API key (never a real one)


class NoNetwork(unittest.TestCase):
    """Base of every test here: a forgotten patch fails fast instead of going online (a first draft once reached the
    real Cloudflare API through a default argument)."""

    def setUp(self):
        super().setUp()

        def refuse(*a, **k):
            raise AssertionError("a test reached the network")
        for target, name in ((ingest, "d1_query"), (ingest, "d1_batch"), (ingest, "tg_upload"), (ingest, "tg_delete"),
                             (ingest, "spotdl_run"), (lastfm, "_OPEN"), (urllib.request, "urlopen"),
                             (socket.socket, "connect")):
            p = mock.patch.object(target, name, refuse)
            p.start()
            self.addCleanup(p.stop)


def lrec(artist, title, duration=None):
    """A record as lastfm_tracks makes it (duration None unless Last.fm gave one)."""
    return lastfm_tracks.make_record({"name": title, "duration": str(duration or 0)}, artist, "Album")


def rank(n, name=None):
    return {"name": name or f"Band {n}", "mbid": None, "playcount": 1000 - n}


def lf_deps(fakes):
    """Fake deps with the matcher contract of #151: a record without a duration is matched by title only."""
    deps = fakes.deps()
    inner = deps.find_match
    deps.find_match = lambda s: (SimpleNamespace(mode=match.MODE_TITLE_ONLY, duration_s=200) if s.get("duration") is None
                                 else inner(s))
    return deps


SECTION = "lastfm_artists:\n  user: some_user\n"


class ParserTest(NoNetwork):
    def test_full_section(self):
        cfg = ingest.parse_source_config(f"""- {GOOD_URL}

lastfm_artists:
  # whole-line comments are fine
  enabled: true
  user: Some-User_1
  years: 3
  max_artists: 50
  rescan_days: 14
  exclude:
    - Various Artists
    - AC/DC
""")
        self.assertEqual(cfg.lastfm, ingest.LastfmConfig(True, "Some-User_1", 3, 50, 14, ("Various Artists", "AC/DC")))
        self.assertEqual((cfg.urls, cfg.followed), ([GOOD_URL], None))

    def test_defaults(self):
        c = ingest.parse_source_config(SECTION).lastfm
        self.assertEqual((c.enabled, c.user, c.years, c.max_artists, c.rescan_days, c.exclude),
                         (True, "some_user", 5, 100, 7, ()))

    def test_empty_exclude_and_a_disabled_section_without_user(self):
        self.assertEqual(ingest.parse_source_config(SECTION + "  exclude:\n").lastfm.exclude, ())
        cfg = ingest.parse_source_config(f"- {GOOD_URL}\nlastfm_artists:\n  enabled: false\n")
        self.assertFalse(cfg.lastfm.enabled)

    def test_coexists_with_followed_artists_in_either_order(self):
        for text in (f"followed_artists:\n  max_artists: 3\n{SECTION}", f"{SECTION}followed_artists:\n  max_artists: 3\n"):
            with self.subTest(text=text):
                cfg = ingest.parse_source_config(text)
                self.assertEqual((cfg.followed.max_artists, cfg.lastfm.user), (3, "some_user"))

    def test_the_repo_sources_file_has_no_lastfm_section(self):
        """The nightly timer reads this file: merging the source must change nothing until the owner opts in (#155)."""
        text = (REPO / "sources.yml").read_text("utf-8")
        self.assertIsNone(ingest.parse_source_config(text).lastfm)
        self.assertNotIn("lastfm_artists", [ln.strip() for ln in text.splitlines() if not ln.lstrip().startswith("#")])

    def test_a_file_without_the_section_parses_as_before(self):
        cfg = ingest.parse_source_config(f"# c\n- {GOOD_URL}\n")
        self.assertEqual((cfg.urls, cfg.followed, cfg.lastfm), ([GOOD_URL], None, None))

    def test_errors(self):
        cases = {
            "unknown key": (SECTION + "  colour: red\n", "unknown key 'colour'"),
            "api key is not a key": (SECTION + "  api_key: abc\n", "unknown key 'api_key'"),
            "repeated key": (SECTION + "  years: 3\n  years: 4\n", "key 'years' given twice"),
            "repeated user": (SECTION + "  user: other\n", "key 'user' given twice"),
            "second section": (SECTION + "lastfm_artists:\n  user: x1\n", "section 'lastfm_artists' given twice"),
            "no user": ("lastfm_artists:\n  years: 3\n", "needs 'user'"),
            "user url": ("lastfm_artists:\n  user: https://www.last.fm/user/x\n", "user must be"),
            "user too short": ("lastfm_artists:\n  user: a\n", "user must be"),
            "user too long": ("lastfm_artists:\n  user: " + "a" * 16 + "\n", "user must be"),
            "user inline comment": ("lastfm_artists:\n  user: name # mine\n", "user must be"),
            "years 0": (SECTION + "  years: 0\n", "years must be a whole number from 1 to 10"),
            "years 11": (SECTION + "  years: 11\n", "years must be"),
            "years text": (SECTION + "  years: five\n", "years must be"),
            "years inline comment": (SECTION + "  years: 5 # five\n", "years must be"),
            "max_artists 0": (SECTION + "  max_artists: 0\n", "max_artists must be a whole number from 1 to 10000"),
            "max_artists 10001": (SECTION + "  max_artists: 10001\n", "max_artists must be"),
            "rescan negative": (SECTION + "  rescan_days: -1\n", "rescan_days must be"),
            "enabled yes": (SECTION + "  enabled: yes\n", "enabled must be true or false"),
            "no value": (SECTION + "  years:\n", "key 'years' has no value"),
            "tab indent": (SECTION + "\tyears: 3\n", "expected '  <key>: <value>'"),
            "three spaces": (SECTION + "   years: 3\n", "expected '  <key>: <value>'"),
            "exclude inline": (SECTION + "  exclude: abc\n", "'exclude:' takes a list"),
            "item outside exclude": (SECTION + "    - Abba\n", "only allowed under 'exclude:'"),
            "item wrong indent": (SECTION + "  exclude:\n      - Abba\n", "expected"),
            "item two spaces": (SECTION + "  exclude:\n  - Abba\n", "key"),  # a two-space dash is not a key either
            "item duplicate": (SECTION + "  exclude:\n    - Abba\n    - Abba\n", "listed twice"),
            "item duplicate normalised": (SECTION + "  exclude:\n    - Beyoncé\n    - beyonce\n    - BEYONCÉ\n", "listed twice"),
            "item quoted": (SECTION + '  exclude:\n    - "Abba"\n', "no quotes"),
            "item inline comment": (SECTION + "  exclude:\n    - Abba # swedes\n", "no inline comment"),
            "item nothing left": (SECTION + "  exclude:\n    - !!!\n", "nothing left"),
            "item too long": (SECTION + "  exclude:\n    - " + "a" * 201 + "\n", "at most 200"),
            "item control char": (SECTION + "  exclude:\n    - Ab\x07ba\n", "printable"),
            "too many items": (SECTION + "  exclude:\n" + "".join(f"    - Band {n}\n" for n in range(501)), "at most 500"),
            "stray line": (SECTION + "color: red\n", "expected '- <playlist url>'"),
            "disabled and no playlists": ("lastfm_artists:\n  enabled: false\n  user: abc\n",
                                          "no playlists and lastfm_artists is not enabled"),
        }
        for name, (text, rx) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ingest.IngestError, rx):
                ingest.parse_source_config(text)

    def test_both_disabled_and_no_playlists(self):
        with self.assertRaisesRegex(ingest.IngestError, "followed_artists and lastfm_artists are not enabled"):
            ingest.parse_source_config("followed_artists:\n  enabled: false\nlastfm_artists:\n  enabled: false\n")

    def test_followed_messages_are_unchanged(self):
        for text, rx in (("followed_artists:\n  colour: red\n", r"\(followed_artists\): unknown key 'colour' \(allowed: enabled, max_artists"),
                         ("followed_artists:\nfollowed_artists:\n", "section 'followed_artists' given twice"),
                         ("followed_artists:\n  exclude:\n    - short\n", "not a Spotify artist id"),
                         ("followed_artists:\n  enabled: false\n", "no playlists and followed_artists is not enabled")):
            with self.subTest(text=text), self.assertRaisesRegex(ingest.IngestError, rx):
                ingest.parse_source_config(text)

    def test_constants_match_the_client(self):
        self.assertEqual(ingest.LASTFM_USER.pattern, lastfm._USER.pattern)
        self.assertEqual(ingest.LASTFM_NAME_MAX, lastfm.NAME_MAX)
        self.assertEqual(ingest.LASTFM_ARTIST_CALLS, lastfm_tracks.CALL_BUDGET)
        self.assertEqual(ingest.LASTFM_ID_PREFIX, lastfm_tracks.ID_PREFIX)

    def test_the_key_is_not_a_sources_key_and_is_not_in_the_config(self):
        self.assertNotIn("key", " ".join(ingest.LASTFM_KEYS).replace("max_artists", ""))
        self.assertNotIn("api_key", ingest.LastfmConfig.__dataclass_fields__)


class StateKeyTest(NoNetwork):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "st.json"

    def test_key_is_stable_normalised_and_accepted_by_the_state(self):
        k = ingest.lastfm_artist_key("Beyoncé")
        self.assertRegex(k, r"lfm:[0-9a-f]{16}")
        self.assertEqual(k, ingest.lastfm_artist_key("  BEYONCÉ "))
        self.assertNotEqual(k, ingest.lastfm_artist_key("Beyonce Knowles"))
        st = ingest.RunState(self.path, today=FixedToday())
        st.record_artist(k)
        loaded = ingest.RunState.load(self.path, None, FixedToday())
        self.assertEqual(set(loaded.artists), {k})

    def test_malformed_keys_are_still_dropped(self):
        self.path.write_text(json.dumps({"version": 1, "tracks": {}, "artists": {"lfm:xyz": "2026-10-10",
                                                                                  "lfm:" + "a" * 16: "2026-10-10"}}))
        with contextlib.redirect_stdout(io.StringIO()):
            loaded = ingest.RunState.load(self.path, None, FixedToday())
        self.assertEqual(set(loaded.artists), {"lfm:" + "a" * 16})


class FlowTest(NoNetwork):
    """run_lastfm with a fake enumerator, the real dedupe and the real run(); the state file is a real temp file."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "work").mkdir()
        self.path = self.dir / "state" / "ingest-state.json"
        self.today = FixedToday("2026-10-10")
        self.records: dict = {}   # artist name -> list of records, or an exception to raise
        self.calls: list = []
        self.existing: set = set()
        self.stored: list = []
        self.f = Fakes(self.dir)

    def enum(self, artist):
        self.calls.append(artist["name"])
        v = self.records[artist["name"]]
        if isinstance(v, Exception):
            raise v
        return v

    def state(self):
        return ingest.RunState.load(self.path, None, self.today)

    def go(self, artists, cfg=None, limits=None, dry=False, deps=None):
        lr = ingest.LastfmRun()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                ingest.run_lastfm(lr, cfg or ingest.LastfmConfig(user="u1"), artists, self.enum, self.stored,
                                  deps or lf_deps(self.f), self.existing, {}, self.dir / "work", dry, limits, self.state())
        finally:
            self.out = out.getvalue()
        return lr

    def test_ingests_in_ranking_order_and_counts(self):
        self.records = {"Band 1": [lrec("Band 1", "a"), lrec("Band 1", "b")], "Band 2": [lrec("Band 2", "c")]}
        lr = self.go([rank(1), rank(2)])
        self.assertEqual(self.calls, ["Band 1", "Band 2"])
        self.assertEqual((lr.listed, lr.scanned, lr.failed, lr.counts["new"], lr.status), (2, 2, 0, 3, "done"))
        self.assertEqual(len(self.f.uploaded), 3)
        self.assertTrue(all(i.startswith("lfm:") for i in self.f.uploaded))
        self.assertEqual(set(json.loads(self.path.read_text())["artists"]),
                         {ingest.lastfm_artist_key("Band 1"), ingest.lastfm_artist_key("Band 2")})

    def test_no_track_title_or_artist_id_in_the_log(self):
        self.records = {"Band 1": [lrec("Band 1", "Secret Title Alpha")]}
        self.go([rank(1)])
        self.assertIn("ingested: Band 1 - Secret Title Alpha", self.out)  # run() labels exactly as for playlists
        self.assertNotIn(ingest.lastfm_artist_key("Band 1"), self.out)

    def test_duplicate_artists_exclude_and_normalised_exclude(self):
        self.records = {"Band 1": [lrec("Band 1", "a")], "Band 3": [lrec("Band 3", "c")]}
        cfg = ingest.LastfmConfig(user="u1", exclude=("various  ARTISTS",))
        lr = self.go([rank(1), rank(1, "BAND 1"), rank(2, "Various Artists"), rank(3)], cfg)
        self.assertEqual(self.calls, ["Band 1", "Band 3"])
        self.assertEqual((lr.listed, lr.excluded), (3, 1))

    def test_rescan_interval(self):
        self.records = {"Band 1": [lrec("Band 1", "a")]}
        self.go([rank(1)])
        self.calls.clear()
        lr = self.go([rank(1)])
        self.assertEqual((self.calls, lr.recent, lr.scanned), ([], 1, 0))
        self.today.day = self.today.day.replace(day=17)  # 7 days later
        self.go([rank(1)])
        self.assertEqual(self.calls, ["Band 1"])
        self.today.day = self.today.day.replace(day=18)
        self.calls.clear()
        self.go([rank(1)], ingest.LastfmConfig(user="u1", rescan_days=0))
        self.assertEqual(self.calls, ["Band 1"])

    def test_cap_stops_inside_an_artist_and_the_next_run_resumes(self):
        self.records = {"Band 1": [lrec("Band 1", "a"), lrec("Band 1", "b")], "Band 2": [lrec("Band 2", "c")]}
        lr = self.go([rank(1), rank(2)], limits=ingest.RunLimits(max_new=1))
        self.assertEqual((lr.status, lr.scanned, lr.not_started, lr.not_attempted, lr.counts["new"]), ("stopped", 0, 1, 1, 1))
        self.assertEqual(self.calls, ["Band 1"])
        self.assertFalse((self.dir / "state" / "ingest-state.json").exists() and
                         "artists" in json.loads(self.path.read_text()))  # no scan recorded
        self.calls.clear()
        lr = self.go([rank(1), rank(2)], limits=ingest.RunLimits(max_new=5))
        self.assertEqual((lr.status, lr.scanned, lr.counts["new"], lr.counts["exists"]), ("done", 2, 2, 1))
        self.assertEqual(len(self.f.uploaded), 3)

    def test_a_limit_already_hit_starts_no_artist(self):
        limits = ingest.RunLimits(max_new=1)
        limits.attempted = 1
        lr = self.go([rank(1), rank(2)], limits=limits)
        self.assertEqual((self.calls, lr.status, lr.not_started), ([], "stopped", 2))
        self.assertIn("2 artist(s) not started", self.out)

    def test_time_limit(self):
        now = [0.0]
        limits = ingest.RunLimits(max_minutes=1, clock=lambda: now[0])
        self.records = {"Band 1": [lrec("Band 1", "a")], "Band 2": [lrec("Band 2", "c")]}
        deps = lf_deps(self.f)
        inner = deps.download

        def slow(s, m, t):
            now[0] += 120  # the first download takes two minutes
            return inner(s, m, t)
        deps.download = slow
        lr = self.go([rank(1), rank(2)], limits=limits, deps=deps)
        self.assertEqual((self.calls, lr.status, lr.scanned, lr.not_started), (["Band 1"], "stopped", 1, 1))

    def test_a_failed_enumeration_does_not_stop_the_others(self):
        self.records = {"Band 1": lastfm.LastfmError("Last.fm kept answering HTTP 500 after 3 retries"),
                        "Band 2": lastfm_tracks.ArtistBudgetExceeded("Last.fm call limit reached"),
                        "Band 3": [lrec("Band 3", "c")]}
        lr = self.go([rank(1), rank(2), rank(3)])
        self.assertEqual((lr.failed, lr.scanned, lr.counts["new"]), (2, 1, 1))
        self.assertIn("enumeration failed: LastfmError: Last.fm kept answering HTTP 500", self.out)
        self.assertNotIn("Traceback", self.out)
        self.assertEqual(set(json.loads(self.path.read_text())["artists"]), {ingest.lastfm_artist_key("Band 3")})

    def test_sign_in_stops_the_whole_run(self):
        self.records = {"Band 1": [lrec("Band 1", "a"), lrec("Band 1", "b")], "Band 2": [lrec("Band 2", "c")]}
        deps = lf_deps(self.f)
        inner = deps.download
        n = []

        def download(s, m, t):
            n.append(1)
            if len(n) == 2:
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
            return inner(s, m, t)
        deps.download = download
        lr = ingest.LastfmRun()
        with self.assertRaises(ingest.YouTubeSignInRequired), contextlib.redirect_stdout(io.StringIO()):
            ingest.run_lastfm(lr, ingest.LastfmConfig(user="u1"), [rank(1), rank(2)], self.enum, [], deps,
                              self.existing, {}, self.dir / "work", False, None, self.state())
        self.assertEqual((lr.status, lr.not_started, lr.counts["new"], self.calls), ("interrupted", 1, 1, ["Band 1"]))
        self.assertFalse(self.path.exists() and "artists" in json.loads(self.path.read_text()))

    def test_dry_run_downloads_nothing_and_records_nothing(self):
        self.records = {"Band 1": [lrec("Band 1", "a")]}
        lr = self.go([rank(1)], dry=True)
        self.assertEqual((self.f.uploaded, lr.counts["dry_run"], lr.scanned), ([], 1, 1))
        self.assertFalse(self.path.exists())

    def test_tracks_stored_under_another_id_are_dropped_before_run(self):
        self.records = {"Band 1": [lrec("Band 1", "Known Song", 200), lrec("Band 1", "Other Song")]}
        self.stored = [("Known Song", "Band 1", 201), ("Unrelated", "Someone", 100)]
        lr = self.go([rank(1)])
        self.assertEqual((lr.dropped_stored, lr.counts["new"], lr.counts["exists"]), (1, 1, 1))
        self.assertEqual(self.f.uploaded, [lrec("Band 1", "Other Song")["song_id"]])
        self.assertEqual(lr.scanned, 1)

    def test_title_only_matches_are_counted_and_marked(self):
        self.records = {"Band 1": [lrec("Band 1", "a"), lrec("Band 1", "b", 200)]}
        deps = self.f.deps()
        deps.find_match = lambda s: SimpleNamespace(mode=match.MODE_TITLE_ONLY if s["duration"] is None else "duration",
                                                   duration_s=215)
        lr = self.go([rank(1)], deps=deps)
        self.assertEqual((lr.counts["new"], lr.counts["matched_title_only"]), (2, 1))
        self.assertEqual(self.out.count("(matched by title only)"), 1)
        self.assertIn("1 matched by title only", ingest.lastfm_summary(lr))

    def test_artists_with_nothing_left_after_normalising_are_skipped_and_counted(self):
        self.assertEqual(ingest.lastfm_artist_key("!!!"), ingest.lastfm_artist_key("???"))  # why they must not run
        self.records = {"Band 1": [lrec("Band 1", "a")]}
        lr = self.go([rank(1, "!!!"), rank(2, "???"), rank(1), rank(3, "   ")])
        self.assertEqual((self.calls, lr.unusable, lr.listed, lr.scanned), (["Band 1"], 3, 1, 1))

    def test_a_name_with_double_colons_cannot_start_a_workflow_command_in_the_log(self):
        self.records = {"::error::x": []}
        self.go([rank(1, "::error::x")])
        for line in self.out.splitlines():
            self.assertNotIn("::", line)

    def test_a_record_already_in_d1_is_not_downloaded_again(self):
        r = lrec("Band 1", "a")
        self.existing.add(r["song_id"])
        self.records = {"Band 1": [r]}
        lr = self.go([rank(1)])
        self.assertEqual((self.f.uploaded, lr.counts["exists"], lr.scanned), ([], 1, 1))


class MainTest(NoNetwork):
    """main() end to end with fakes for the ranking, the enumerator, D1, spotdl and the downloader."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "work").mkdir()
        self.f = Fakes(self.dir)
        self.records = {"Band 1": [lrec("Band 1", "a"), lrec("Band 1", "b")], "Band 2": [lrec("Band 2", "c")]}
        self.ranking = [rank(1), rank(2)]
        self.enum_calls, self.d1_calls, self.stored_rows, self.rank_args = [], [], [], []

    def run_main(self, sources, argv=(), key=KEY, ranking_error=None, playlist_songs=None, deps=None, followed=None,
                 use_real_ranking=False, ranking_fn=None, stored_error=None):
        (self.dir / "s.yml").write_text(sources)
        env = {k: "x" for k in ingest.REQUIRED_ENV}

        def enumerate_artist(artist):
            self.enum_calls.append(artist["name"])
            v = self.records[artist["name"]]
            if isinstance(v, Exception):
                raise v
            return v

        def top_artists(client, user, years=5, now=None, max_artists=100, out=None):
            self.rank_args.append((user, years, max_artists))
            if ranking_fn:
                return ranking_fn(client, user, years, now, max_artists, out)
            if ranking_error:
                raise ranking_error
            return list(self.ranking)

        def fake_spotdl(args, cwd):
            Path(args[args.index("--save-file") + 1]).write_text(json.dumps(playlist_songs or []))
            return [], ""

        def d1(sql, params=None):
            self.d1_calls.append(sql)
            if sql == ingest.STORED_ROWS_SQL:
                if stored_error:
                    raise stored_error
                return [{"title": t, "artist": a, "duration_s": d} for t, a, d in self.stored_rows]
            return []

        res = self.dir / "r.json"
        full = ["ingest.py", "--sources", str(self.dir / "s.yml"), "--result-json", str(res),
                "--state-file", str(self.dir / "st.json"), "--pace-seconds", "0", "--pace-jitter-seconds", "0", *argv]
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", full), \
                mock.patch.object(ingest, "d1_query", d1), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "spotdl_run", fake_spotdl), \
                mock.patch.object(ingest, "real_deps", lambda ytm, thr: deps or lf_deps(self.f)), \
                mock.patch.object(ingest, "real_lastfm_enumerator", lambda client: enumerate_artist), \
                mock.patch.object(ingest, "real_enumerator", lambda cid, secret: lambda a, g, m: []), \
                mock.patch.object(lastfm, "top_artists", lastfm.top_artists if use_real_ranking else top_artists), \
                mock.patch.object(spotify_user, "get_access_token", lambda: "t"), \
                mock.patch.object(spotify_user, "followed_artists", lambda tok: list(followed or [])), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                contextlib.redirect_stdout(out):
            os.environ.pop("LASTFM_API_KEY", None)  # restored by patch.dict
            os.environ["SPOTIFY_USER_REFRESH_TOKEN"] = "r"
            if key is not None:
                os.environ["LASTFM_API_KEY"] = key
            code = ingest.main()
        self.out = out.getvalue()
        return code, json.loads(res.read_text()) if res.exists() else None

    def test_lastfm_only_file_ingests_and_reports(self):
        code, r = self.run_main(SECTION)
        self.assertEqual((code, r["stop_reason"], r["failure_class"], r["playlists"]), (0, "done", "none", []))
        self.assertEqual(len(self.f.uploaded), 3)
        self.assertEqual(self.rank_args, [("some_user", 5, 100)])
        sec = r["lastfm_artists"]
        self.assertEqual((sec["status"], sec["added"], r["counts"]["added"]), ("done", 3, 3))
        self.assertEqual(sec["artists"], {"listed": 2, "excluded": 0, "skipped_recent": 0, "scanned": 2, "failed": 0,
                                          "not_started": 0, "dropped_stored": 0,
                                          "unusable": 0})
        self.assertIn("lastfm artists: 2 ranked", self.out)
        self.assertIn("done: 3 new", self.out)
        self.assertNotIn(KEY, self.out + json.dumps(r))
        self.assertNotIn("Band 1", json.dumps(r))  # counts only
        self.assertEqual(self.d1_calls.count(ingest.STORED_ROWS_SQL), 1)

    def test_the_config_reaches_the_ranking(self):
        self.run_main("lastfm_artists:\n  user: abc\n  years: 2\n  max_artists: 7\n")
        self.assertEqual(self.rank_args, [("abc", 2, 7)])

    def test_missing_key_is_a_config_error_with_exit_2_before_any_work(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", key=None, playlist_songs=[song(10)])
        self.assertEqual(code, 2)
        self.assertEqual((r["failure_class"], r["exit_code"], r["stop_reason"]), ("config", 2, None))
        self.assertIn("LASTFM_API_KEY is not set", self.out)
        self.assertIn("exit code 2", self.out)
        self.assertEqual((self.d1_calls, self.enum_calls, self.rank_args, self.f.uploaded), ([], [], [], []))
        self.assertFalse((self.dir / "st.json").exists())

    def test_malformed_key_is_a_config_error_and_the_value_is_never_printed(self):
        code, r = self.run_main(SECTION, key="not a key!")
        self.assertEqual((code, r["failure_class"], self.d1_calls, self.f.uploaded), (2, "config", [], []))
        self.assertIn("LASTFM_API_KEY", self.out)
        self.assertNotIn("not a key!", self.out)

    def test_a_failing_ranking_never_stops_the_playlists(self):
        """Outage, 5xx, rate limit: the playlist is ingested first, the phase is a counted failure (exit 1)."""
        for msg in ("Last.fm kept answering HTTP 503 after 3 retries", "Last.fm rate limit exceeded after 3 retries",
                    "malformed Last.fm response: `weeklyartistchart` is missing"):
            with self.subTest(msg):
                self.d1_calls.clear()
                self.f.uploaded.clear()
                code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", ranking_error=lastfm.LastfmError(msg),
                                        playlist_songs=[song(10)])
                self.assertEqual((code, r["failure_class"], r["stop_reason"]), (1, "partial", "done"))
                self.assertEqual(self.f.uploaded, [sid(10)])
                self.assertEqual((r["playlists"][0]["status"], r["playlists"][0]["added"]), ("done", 1))
                self.assertEqual(r["lastfm_artists"]["status"], "failed")
                self.assertEqual(r["counts"]["added"], 1)
                self.assertIn(msg, self.out)
                self.assertIn("the Last.fm phase failed", self.out)
                self.assertNotIn("Traceback", self.out)
                self.assertNotIn(ingest.STORED_ROWS_SQL, self.d1_calls)
                self.assertEqual(self.enum_calls, [])

    def test_config_refusals_exit_2_but_only_after_the_playlists_ran(self):
        cases = [lastfm.LastfmError("Last.fm: user not found (error 6)"), lastfm.LastfmError("invalid key (error 10)"),
                 lastfm.LastfmError("profile is private (error 17)"), lastfm.LastfmError("key suspended (error 26)"),
                 lastfm.LastfmError("no weekly charts in the window: the profile has no listening history there, or it is private"),
                 lastfm.LastfmError("the weekly charts hold no artists: the listening history is empty or private")]
        for e, code_ in zip(cases, (6, 10, 17, 26, None, None)):
            e.code = code_
            with self.subTest(str(e)):
                self.f.uploaded.clear()
                code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", ranking_error=e, playlist_songs=[song(10)])
                self.assertEqual((code, r["failure_class"], r["exit_code"]), (2, "config", 2))
                self.assertEqual(self.f.uploaded, [sid(10)])  # the playlist ran and is reported
                self.assertEqual(r["playlists"][0]["added"], 1)
                self.assertEqual(r["lastfm_artists"]["status"], "failed")
                self.assertIn(str(e), self.out)
                self.assertIn("exit code 2", self.out)

    def test_the_real_top_artists_classifies_an_empty_profile_as_config(self):
        """No fake ranking: the real top_artists with a fake fetch answering an empty chart list."""
        def fetch(url, timeout):
            return 200, {"weeklychartlist": {"chart": []}}
        real = lastfm.LastfmClient
        with mock.patch.object(lastfm, "LastfmClient", lambda k, **kw: real(k, fetch=fetch, sleep=lambda s: None, **kw)):
            code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", use_real_ranking=True, playlist_songs=[song(10)])
        self.assertEqual((code, r["failure_class"]), (2, "config"))
        self.assertEqual(self.f.uploaded, [sid(10)])

    def test_error_text_never_contains_the_key(self):
        err = lastfm.LastfmError(f"boom {KEY}")
        code, _ = self.run_main(SECTION, ranking_error=err)
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, self.out)

    def test_the_real_client_gets_the_key_only_in_the_request_and_after_the_playlists(self):
        """No fake for the ranking: the real client gets a fake fetch that answers an invalid-key error."""
        seen = []

        def fetch(url, timeout):
            seen.append(url)
            return 200, {"error": 10, "message": f"Invalid API key {KEY}"}
        real = lastfm.LastfmClient
        with mock.patch.object(lastfm, "LastfmClient", lambda k, **kw: real(k, fetch=fetch, sleep=lambda s: None, **kw)):
            code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", use_real_ranking=True, playlist_songs=[song(10)])
        self.assertEqual((code, r["failure_class"]), (2, "config"))
        self.assertEqual((len(seen), self.f.uploaded), (1, [sid(10)]))
        self.assertIn(KEY, seen[0])
        self.assertNotIn(KEY, self.out + json.dumps(r))

    def test_max_minutes_also_bounds_the_ranking(self):
        now = [0.0]
        calls = []

        class FakeClient:
            def __init__(self, key, **kw):
                pass

            def call(self, method, **params):
                calls.append(method)
                now[0] += 30  # every request takes 30 s
                return {}

        def top(client, user, years=5, now=None, max_artists=100, out=None):
            for _ in range(50):
                client.call("user.getWeeklyArtistChart", user=user)
            return [rank(1)]
        orig = ingest.RunLimits
        with mock.patch.object(lastfm, "LastfmClient", FakeClient), \
                mock.patch.object(ingest, "RunLimits", lambda n, m: orig(n, m, clock=lambda: now[0])):
            code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", ["--max-minutes", "1"], playlist_songs=[song(10)],
                                    ranking_fn=top)
        self.assertEqual((code, r["stop_reason"], r["lastfm_artists"]["status"]), (0, "time", "stopped"))
        self.assertLessEqual(len(calls), 3)  # not 50
        self.assertEqual(self.f.uploaded, [sid(10)])
        self.assertEqual(self.enum_calls, [])

    def test_a_limit_already_hit_skips_the_ranking_altogether(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", ["--max-new", "1"], playlist_songs=[song(10), song(11)])
        self.assertEqual((code, r["stop_reason"], r["lastfm_artists"]["status"], self.rank_args), (0, "cap", "stopped", []))

    def test_a_d1_error_reading_the_stored_rows_is_a_counted_failure_not_a_traceback(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", playlist_songs=[song(10)],
                                stored_error=ingest.IngestError(f"D1 API error 500 {KEY}"))
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (1, "partial", "done"))
        self.assertEqual(self.f.uploaded, [sid(10)])
        self.assertEqual((r["lastfm_artists"]["status"], r["counts"]["added"]), ("failed", 1))
        self.assertIn("ERROR lastfm phase failed: IngestError: D1 API error 500", self.out)
        self.assertNotIn(KEY, self.out)
        self.assertEqual(self.enum_calls, [])

    def test_a_sign_in_during_the_phase_is_not_swallowed_by_the_failure_catch(self):
        deps = lf_deps(self.f)
        deps.download = mock.Mock(side_effect=ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)"))
        code, r = self.run_main(SECTION, deps=deps)
        self.assertEqual((code, r["failure_class"]), (3, "youtube-sign-in"))

    def test_disabled_section_needs_no_key_and_runs_nothing_extra(self):
        code, r = self.run_main(f"- {GOOD_URL}\nlastfm_artists:\n  enabled: false\n", key=None, playlist_songs=[song(10)])
        self.assertEqual((code, self.rank_args, self.enum_calls), (0, [], []))
        self.assertNotIn("lastfm_artists", r)
        self.assertNotIn(ingest.STORED_ROWS_SQL, self.d1_calls)

    def test_playlist_only_file_never_reads_stored_rows_or_the_key(self):
        code, r = self.run_main(f"- {GOOD_URL}\n", key=None, playlist_songs=[song(10)])
        self.assertEqual((code, self.rank_args, self.enum_calls), (0, [], []))
        self.assertNotIn(ingest.STORED_ROWS_SQL, self.d1_calls)
        self.assertEqual(set(r), {"failure_class", "exit_code", "stop_reason", "playlists", "counts"})
        self.assertNotIn("lastfm", self.out)

    def test_playlist_runs_first_and_lastfm_sees_what_it_added(self):
        self.records = {"Band 1": [lrec("Band 1", "a")], "Band 2": []}
        self.stored_rows = [("a", "Band 1", None)]  # what D1 would hold after the playlist phase
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", playlist_songs=[song(10)])
        self.assertEqual(code, 0)
        self.assertEqual(self.f.uploaded, [sid(10)])
        self.assertEqual((r["playlists"][0]["added"], r["lastfm_artists"]["added"], r["counts"]["added"]), (1, 0, 1))
        self.assertEqual(r["lastfm_artists"]["artists"]["dropped_stored"], 1)
        self.assertEqual(self.d1_calls.index(ingest.STORED_ROWS_SQL) > self.d1_calls.index("SELECT spotify_id FROM tracks"), True)
        self.assertEqual(self.d1_calls.count(ingest.STORED_ROWS_SQL), 1)

    def test_coexists_with_followed_artists(self):
        code, r = self.run_main(f"followed_artists:\n{SECTION}", followed=[art(1)])
        self.assertEqual(code, 0)
        self.assertEqual((r["followed_artists"]["artists"]["listed"], r["lastfm_artists"]["artists"]["listed"]), (1, 2))
        self.assertEqual(r["counts"]["added"], 3)
        self.assertIn("followed artists:", self.out)
        self.assertIn("lastfm artists:", self.out)

    def test_playlists_and_lastfm_share_the_cap(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", ["--max-new", "2"], playlist_songs=[song(10)])
        self.assertEqual((code, r["stop_reason"]), (0, "cap"))
        self.assertEqual(len(self.f.uploaded), 2)
        self.assertEqual((r["playlists"][0]["added"], r["lastfm_artists"]["added"], r["lastfm_artists"]["status"]),
                         (1, 1, "stopped"))
        self.assertEqual(r["lastfm_artists"]["not_attempted"], 1)

    def test_a_failed_enumeration_gives_exit_1_and_the_others_ingest(self):
        self.records["Band 1"] = lastfm.LastfmError("Last.fm kept answering HTTP 500 after 3 retries")
        code, r = self.run_main(SECTION)
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (1, "partial", "done"))
        self.assertEqual((r["lastfm_artists"]["artists"]["failed"], r["lastfm_artists"]["artists"]["scanned"]), (1, 1))
        self.assertIn("1 artist(s) could not be enumerated", self.out)

    def test_sign_in_exits_3_and_keeps_the_playlists_result(self):
        deps = lf_deps(self.f)
        inner = deps.download

        def download(s, m, t):
            if s["song_id"].startswith("lfm:"):
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
            return inner(s, m, t)
        deps.download = download
        code, r = self.run_main(f"- {GOOD_URL}\n{SECTION}", playlist_songs=[song(10)], deps=deps)
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (3, "youtube-sign-in", "sign-in"))
        self.assertEqual([p["status"] for p in r["playlists"]], ["done"])
        self.assertEqual(self.enum_calls, ["Band 1"])
        self.assertEqual((r["lastfm_artists"]["status"], r["lastfm_artists"]["artists"]["not_started"]), ("interrupted", 1))

    def test_second_run_skips_recent_artists(self):
        self.run_main(SECTION)
        before = list(self.f.uploaded)
        self.enum_calls.clear()
        code, r = self.run_main(SECTION)
        self.assertEqual((code, self.enum_calls, self.f.uploaded), (0, [], before))
        self.assertEqual(r["lastfm_artists"]["artists"]["skipped_recent"], 2)

    def test_dry_run_writes_no_state_and_no_artist_progress(self):
        code, r = self.run_main(SECTION, ["--dry-run"])
        self.assertEqual((code, self.f.uploaded, r["counts"]["added"]), (0, [], 0))
        self.assertIn("dry-run would ingest", self.out)
        self.assertFalse((self.dir / "st.json").exists())

    def test_title_only_matches_reach_the_result_and_the_total(self):
        deps = self.f.deps()
        deps.find_match = lambda s: SimpleNamespace(mode=match.MODE_TITLE_ONLY, duration_s=200)
        code, r = self.run_main(SECTION, deps=deps)
        self.assertEqual((code, r["lastfm_artists"]["matched_title_only"], r["counts"]["matched_title_only"]), (0, 3, 3))
        self.assertIn("3 matched by title only", self.out)


class SpotdlEnvTest(NoNetwork):
    """The spotdl child process must never see the Last.fm key (it needs no Last.fm access)."""

    CODE = "import os; print('SEEN' if 'LASTFM_API_KEY' in os.environ else 'HIDDEN')"

    def child_says(self, hidden):
        env = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "csecret", "LASTFM_API_KEY": KEY}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
                mock.patch.object(ingest, "SPOTDL_CMD", [sys.executable, "-c", self.CODE]), \
                mock.patch.object(ingest, "SPOTDL_HIDDEN_ENV", hidden):
            _, tail = REAL_SPOTDL_RUN(["save", "u"], Path(d))
        return tail

    def test_the_child_does_not_see_the_key(self):
        self.assertEqual(self.child_says(ingest.SPOTDL_HIDDEN_ENV), "HIDDEN")
        self.assertIn("LASTFM_API_KEY", ingest.SPOTDL_HIDDEN_ENV)

    def test_the_check_can_fail(self):
        self.assertEqual(self.child_says(ingest.SPOTDL_HIDDEN_ENV - {"LASTFM_API_KEY"}), "SEEN")


class LazyImportTest(unittest.TestCase):
    """A run without an enabled lastfm_artists section must not even load ingest/lastfm*.py."""

    SCRIPT = """
import contextlib, io, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, sys.argv[1])
import ingest
d = Path(tempfile.mkdtemp())
(d / "s.yml").write_text(sys.argv[2])
def d1(sql, params=None):
    return []
def spotdl(args, cwd):
    Path(args[args.index("--save-file") + 1]).write_text("[]")
    return [], ""
deps = ingest.Deps(lambda s: None, None, None, None, None, None)
env = {k: "x" for k in ingest.REQUIRED_ENV}
with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \\
        mock.patch.object(sys, "argv", ["ingest.py", "--sources", str(d / "s.yml"), "--state-file", str(d / "st.json")]), \\
        mock.patch.object(ingest, "d1_query", d1), mock.patch.object(ingest, "js_runtime_problem", return_value=None), \\
        mock.patch.object(ingest, "spotdl_run", spotdl), mock.patch.object(ingest, "real_deps", lambda y, t: deps), \\
        mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \\
        contextlib.redirect_stdout(io.StringIO()):
    code = ingest.main()
    seen = sorted(m for m in sys.modules if m.startswith("lastfm"))  # inside: patch.dict(sys.modules) undoes imports
print(json.dumps({"code": code, "lastfm": seen}))
"""

    def loaded(self, sources):
        proc = subprocess.run([sys.executable, "-I", "-c", self.SCRIPT, str(Path(__file__).resolve().parent), sources],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_playlist_only_run_loads_no_lastfm_module(self):
        res = self.loaded(f"- {GOOD_URL}\n")
        self.assertEqual(res, {"code": 0, "lastfm": []})

    def test_disabled_section_loads_no_lastfm_module(self):
        res = self.loaded(f"- {GOOD_URL}\nlastfm_artists:\n  enabled: false\n")
        self.assertEqual(res, {"code": 0, "lastfm": []})

    def test_importing_ingest_does_not_import_lastfm(self):
        proc = subprocess.run([sys.executable, "-I", "-c",
                               "import sys; sys.path.insert(0, sys.argv[1]); import ingest; "
                               "print(sorted(m for m in sys.modules if m.startswith('lastfm')))",
                               str(Path(__file__).resolve().parent)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_enabled_section_loads_it_lazily_without_an_import_cycle(self):
        """lastfm imports `redact` from ingest: loading it from inside execute() works when ingest runs as a script."""
        proc = subprocess.run([sys.executable, "-I", "-c",
                               "import sys; sys.path.insert(0, sys.argv[1]); import lastfm, ingest; print('ok')",
                               str(Path(__file__).resolve().parent)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "ok", proc.stderr[-300:])


if __name__ == "__main__":
    unittest.main()

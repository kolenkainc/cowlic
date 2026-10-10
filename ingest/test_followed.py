"""Tests for the followed_artists source (#121). Everything is faked: no network, no real downloader, no login."""
import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import artist_tracks
import ingest
import spotify_user
from test_ingest import GOOD_URL, GOOD_URL2, FixedToday, Fakes, sid, song

REPO = Path(__file__).resolve().parent.parent


def aid(n):
    return f"ar{n:020d}"


def art(n):
    return {"id": aid(n), "name": f"Artist {n}"}


FULL = f"""# playlists first
- {GOOD_URL}

followed_artists:
  # whole-line comments are fine
  enabled: true
  max_artists: 20
  rescan_days: 14
  groups: album,single,compilation
  market: DE
  exclude:
    - {aid(1)}
    - {aid(2)}
- {GOOD_URL2}
"""


class SourceConfigTest(unittest.TestCase):
    def test_full_section(self):
        cfg = ingest.parse_source_config(FULL)
        self.assertEqual(cfg.urls, [GOOD_URL, GOOD_URL2])  # a playlist after the section still counts
        self.assertEqual(cfg.followed, ingest.FollowedConfig(True, 20, 14, ("album", "single", "compilation"), "DE",
                                                             (aid(1), aid(2))))

    def test_defaults(self):
        cfg = ingest.parse_source_config("followed_artists:\n")
        self.assertEqual(cfg.followed, ingest.FollowedConfig())
        self.assertEqual((cfg.followed.rescan_days, cfg.followed.groups, cfg.followed.max_artists,
                          cfg.followed.exclude), (7, ("album", "single"), None, ()))
        self.assertEqual(cfg.urls, [])

    def test_empty_exclude_list_and_a_section_only_file_are_valid(self):
        self.assertEqual(ingest.parse_source_config("followed_artists:\n  exclude:\n").followed.exclude, ())

    def test_flat_playlist_file_is_unchanged(self):
        text = f"# c\n\n- {GOOD_URL}\n  - {GOOD_URL2}?si=abc123\n- {GOOD_URL}\n"
        cfg = ingest.parse_source_config(text)
        self.assertEqual((cfg.urls, cfg.followed), ([GOOD_URL, GOOD_URL2 + "?si=abc123"], None))
        self.assertEqual(ingest.parse_sources(text), cfg.urls)

    def test_the_repo_sources_file_has_no_followed_section(self):
        """The nightly timer reads this file: merging the source must change nothing until the owner opts in."""
        self.assertIsNone(ingest.parse_source_config((REPO / "sources.yml").read_text("utf-8")).followed)
        self.assertNotIn("followed_artists", [ln.strip() for ln in (REPO / "sources.yml").read_text("utf-8").splitlines()
                                              if not ln.lstrip().startswith("#")])

    def test_flat_file_errors_are_unchanged(self):
        for text, rx in (("", "no playlists"), ("# c\n", "no playlists"), ("key: value\n", "sources line 1"),
                         (f"- {GOOD_URL}\n  followed_artists:\n", "sources line 2")):
            with self.subTest(text=text), self.assertRaisesRegex(ingest.IngestError, rx):
                ingest.parse_source_config(text)

    def bad(self, body, rx, prefix=f"- {GOOD_URL}\nfollowed_artists:\n"):
        with self.assertRaisesRegex(ingest.IngestError, rx):
            ingest.parse_source_config(prefix + body)

    def test_unknown_key(self):
        self.bad("  playlist: x\n", r"sources line 3 \(followed_artists\): unknown key 'playlist'")
        self.bad("  Enabled: true\n", "expected '  <key>")  # the key regex is lower-case only

    def test_duplicates(self):
        self.bad("  enabled: true\n  enabled: false\n", "line 4 .*'enabled' given twice")
        self.bad(f"  exclude:\n    - {aid(1)}\n    - {aid(1)}\n", "line 5 .*listed twice")
        self.bad("  exclude:\n  exclude:\n", "given twice")
        with self.assertRaisesRegex(ingest.IngestError, "section 'followed_artists' given twice"):
            ingest.parse_source_config("followed_artists:\n  enabled: true\nfollowed_artists:\n")

    def test_bad_values(self):
        for body in ("  enabled: yes\n", "  enabled: True\n", "  enabled:\n", "  max_artists: 0\n", "  max_artists: -1\n",
                     "  max_artists: 10001\n", "  max_artists: 1.5\n", "  max_artists: abc\n", "  rescan_days: -1\n",
                     "  rescan_days: 3651\n", "  groups: album,album\n", "  groups: album,single,\n", "  groups: track\n",
                     "  groups: \n", "  groups: Album\n", "  market: us\n", "  market: USA\n", "  market:\n",
                     "  enabled: true # note\n"):
            with self.subTest(body=body), self.assertRaises(ingest.IngestError):
                ingest.parse_source_config("followed_artists:\n" + body)

    def test_structure_errors(self):
        self.bad(f"  - {aid(1)}\n", "expected '  <key>")                      # item with key indent
        self.bad(f"    - {aid(1)}\n", "only allowed under 'exclude:'")       # item before any exclude
        self.bad(f"  enabled: true\n    - {aid(1)}\n", "only allowed under 'exclude:'")
        self.bad(f"  exclude:\n    - {aid(1)}\n  max_artists: 3\n    - {aid(2)}\n", "only allowed under 'exclude:'")
        self.bad("  exclude: []\n", "takes a list on the following lines")
        self.bad("  exclude:\n    - short\n", "not a Spotify artist id")
        self.bad(f"  exclude:\n    - {aid(1)} # c\n", "expected '  <key>")
        self.bad(f"  exclude:\n      - {aid(1)}\n", "expected '  <key>")     # wrong indent
        self.bad("\tenabled: true\n", "expected '  <key>")
        self.bad("   enabled: true\n", "expected '  <key>")
        self.bad("  enabled : true\n", "expected '  <key>")
        self.bad("  enabled:  true\n", "expected .  <key>")

    def test_an_unindented_line_ends_the_section(self):
        cfg = ingest.parse_source_config(f"followed_artists:\n  max_artists: 3\n- {GOOD_URL}\n")
        self.assertEqual((cfg.urls, cfg.followed.max_artists), ([GOOD_URL], 3))
        with self.assertRaisesRegex(ingest.IngestError, "sources line 3: expected '- <playlist url>'"):
            ingest.parse_source_config("followed_artists:\n  max_artists: 3\nmax_artists: 4\n")

    def test_disabled_section_needs_a_playlist(self):
        self.assertEqual(ingest.parse_source_config(f"- {GOOD_URL}\nfollowed_artists:\n  enabled: false\n").urls,
                         [GOOD_URL])
        with self.assertRaisesRegex(ingest.IngestError, "no playlists and followed_artists is not enabled"):
            ingest.parse_source_config("followed_artists:\n  enabled: false\n")

    def test_group_names_match_artist_tracks(self):
        self.assertEqual(ingest.FOLLOWED_GROUPS, artist_tracks.ALL_GROUPS)
        d = ingest.FollowedConfig()
        self.assertEqual((d.groups, d.market), (artist_tracks.DEFAULT_GROUPS, artist_tracks.DEFAULT_MARKET))
        self.assertEqual(artist_tracks.parse_groups(d.groups), d.groups)


class FollowedFlowTest(unittest.TestCase):
    """run_followed with a fake enumerator, the real dedupe and the real run(); the state file is a real temp file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "work").mkdir()
        self.path = self.dir / "state" / "ingest-state.json"
        self.today = FixedToday("2026-10-10")
        self.songs: dict = {}          # artist id -> list of records, or an exception to raise
        self.calls: list = []          # artist ids the enumerator was asked for
        self.existing: set = set()     # stands for D1 across runs
        self.f = Fakes(self.dir)

    def enum(self, artist, groups, market):
        self.calls.append((artist, groups, market))
        v = self.songs[artist]
        if isinstance(v, Exception):
            raise v
        return v

    def state(self):
        return ingest.RunState.load(self.path, None, self.today)

    def go(self, artists, cfg=None, limits=None, dry=False, deps=None):
        fr = ingest.FollowedRun()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                ingest.run_followed(fr, cfg or ingest.FollowedConfig(), artists, self.enum, deps or self.f.deps(),
                                    self.existing, {}, self.dir / "work", dry, limits, self.state())
        finally:
            self.out = out.getvalue()
        return fr

    def asked(self):
        return [c[0] for c in self.calls]

    def test_ingests_every_artist_in_followed_order_and_passes_the_config(self):
        self.songs = {aid(1): [song(1), song(2)], aid(2): [song(3)], aid(3): [song(4)]}
        fr = self.go([art(2), art(1), art(3)], ingest.FollowedConfig(groups=("album",), market="GB"))
        self.assertEqual(self.asked(), [aid(2), aid(1), aid(3)])
        self.assertEqual(self.calls[0], (aid(2), ("album",), "GB"))
        self.assertEqual(self.f.uploaded, [sid(3), sid(1), sid(2), sid(4)])
        self.assertEqual((fr.counts["new"], fr.scanned, fr.listed, fr.failed, fr.status), (4, 3, 3, 0, "done"))
        self.assertEqual(set(self.state().artists), {aid(1), aid(2), aid(3)})

    def test_duplicate_artist_ids_across_pages_are_enumerated_once(self):
        self.songs = {aid(1): [song(1)], aid(2): [song(2)]}
        fr = self.go([art(1), art(2), art(1), {"id": aid(2), "name": "other name"}])
        self.assertEqual(self.asked(), [aid(1), aid(2)])
        self.assertEqual(fr.listed, 2)

    def test_cross_release_duplicates_are_downloaded_once(self):
        single = dict(song(1), name="Hit", album_type="single", release_date="2020-01-01")
        album = dict(song(2), name="Hit - Remastered", album_type="album", release_date="2021-05-05")
        self.songs = {aid(1): [album, single]}
        fr = self.go([art(1)])
        self.assertEqual(self.f.uploaded, [sid(1)])  # the earliest release wins, the album copy is not downloaded
        self.assertEqual(fr.counts["new"], 1)
        self.assertIn("unique 1, dropped 1", self.out)

    def test_a_recording_already_stored_under_another_release_is_not_downloaded_again(self):
        single = dict(song(1), name="Hit", album_type="single", release_date="2020-01-01")
        deluxe = dict(song(2), name="Hit", album_type="album", release_date="2021-05-05")
        self.songs = {aid(1): [single, deluxe]}
        self.existing.add(sid(2))  # the deluxe copy came in earlier (a playlist); dedupe keeps the single
        fr = self.go([art(1)])
        self.assertEqual(self.f.uploaded, [])
        self.assertEqual((fr.counts["new"], fr.counts["exists"]), (0, 1))
        self.assertIn("1 already stored under another release", self.out)

    def test_exclude_list(self):
        self.songs = {aid(1): [song(1)], aid(2): [song(2)], aid(3): [song(3)]}
        fr = self.go([art(1), art(2), art(3)], ingest.FollowedConfig(exclude=(aid(2),)))
        self.assertEqual(self.asked(), [aid(1), aid(3)])
        self.assertEqual((fr.excluded, fr.listed, fr.scanned), (1, 3, 2))
        self.assertNotIn(aid(2), self.state().artists)

    def test_max_artists_starts_that_many_and_the_next_run_continues(self):
        self.songs = {aid(n): [song(n)] for n in range(1, 6)}
        cfg = ingest.FollowedConfig(max_artists=2)
        fr = self.go([art(n) for n in range(1, 6)], cfg)
        self.assertEqual((self.asked(), fr.deferred), ([aid(1), aid(2)], 3))
        self.assertIn("max_artists 2 reached; 3 artist(s) not started", self.out)
        self.calls.clear()
        fr = self.go([art(n) for n in range(1, 6)], cfg)  # 1 and 2 were scanned: they do not use the quota
        self.assertEqual((self.asked(), fr.recent, fr.deferred), ([aid(3), aid(4)], 2, 1))
        self.calls.clear()
        self.go([art(n) for n in range(1, 6)], cfg)
        self.assertEqual(self.asked(), [aid(5)])
        self.assertEqual(len(self.f.uploaded), 5)

    def test_a_failed_enumeration_does_not_stop_the_others(self):
        self.songs = {aid(1): artist_tracks.ArtistTracksError("Spotify request failed: HTTP 500"), aid(2): [song(2)],
                      aid(3): OSError("boom with details"), aid(4): [song(4)]}
        fr = self.go([art(1), art(2), art(3), art(4)])
        self.assertEqual((fr.failed, fr.scanned, fr.counts["new"]), (2, 2, 2))
        self.assertEqual(set(self.state().artists), {aid(2), aid(4)})  # a failed artist is retried by the next run
        self.assertIn("ERROR artist Artist 1: enumeration failed: ArtistTracksError: Spotify request failed: HTTP 500",
                      self.out)
        self.assertIn("enumeration failed: OSError\n", self.out)
        self.assertNotIn("boom with details", self.out)

    def test_cap_stops_inside_an_artist_and_the_next_run_resumes(self):
        self.songs = {aid(1): [song(1), song(2)], aid(2): [song(n) for n in range(3, 7)], aid(3): [song(7)]}
        artists = [art(1), art(2), art(3)]
        fr = self.go(artists, limits=ingest.RunLimits(max_new=3))
        self.assertEqual(self.f.uploaded, [sid(1), sid(2), sid(3)])
        self.assertEqual((fr.status, fr.scanned, fr.not_started, fr.not_attempted), ("stopped", 1, 1, 3))
        self.assertEqual(set(self.state().artists), {aid(1)})  # the unfinished artist is not recorded
        self.assertIn("stopped: cap", self.out)
        self.calls.clear()
        fr = self.go(artists, limits=ingest.RunLimits(max_new=10))  # run 2: artist 1 is recent, 2 resumes
        self.assertEqual(self.asked(), [aid(2), aid(3)])
        self.assertEqual(self.f.uploaded, [sid(n) for n in range(1, 8)])  # nothing twice
        self.assertEqual((fr.status, fr.recent, fr.counts["exists"], fr.counts["new"]), ("done", 1, 1, 4))
        self.calls.clear()
        self.go(artists, limits=ingest.RunLimits(max_new=10))  # run 3: everything is recent
        self.assertEqual(self.calls, [])

    def test_the_cap_is_reached_exactly_between_two_artists(self):
        self.songs = {aid(1): [song(1)], aid(2): [song(2)]}
        fr = self.go([art(1), art(2)], limits=ingest.RunLimits(max_new=1))
        self.assertEqual((self.asked(), fr.scanned, fr.not_started, fr.status), ([aid(1)], 1, 1, "stopped"))
        self.assertEqual(set(self.state().artists), {aid(1)})

    def test_a_limit_already_hit_in_the_playlist_phase_starts_no_artist(self):
        limits = ingest.RunLimits(max_new=1)
        limits.attempted = 1
        limits.stop("cap", 7)
        fr = self.go([art(1)], limits=limits)
        self.assertEqual((self.calls, fr.status, fr.not_started), ([], "stopped", 1))
        self.assertEqual(limits.not_attempted, 7)  # not overwritten

    def test_time_limit(self):
        now = [0.0]
        self.songs = {aid(1): [song(1)], aid(2): [song(2)]}
        limits = ingest.RunLimits(max_minutes=1, clock=lambda: now[0])
        deps = self.f.deps()
        inner = deps.upload_audio
        deps.upload_audio = lambda s, p: (now.__setitem__(0, 61.0), inner(s, p))[1]
        fr = self.go([art(1), art(2)], limits=limits, deps=deps)
        self.assertEqual((self.asked(), limits.stop_reason, fr.status), ([aid(1)], "time", "stopped"))

    def test_rescan_interval_with_a_clock(self):
        self.songs = {aid(1): [song(1)]}
        cfg = ingest.FollowedConfig(rescan_days=7)
        self.go([art(1)], cfg)
        self.assertEqual(self.state().artists[aid(1)].isoformat(), "2026-10-10")
        self.songs[aid(1)] = [song(1), song(2)]  # a new release arrives
        for day in ("2026-10-11", "2026-10-16"):  # 1 and 6 days later: not looked at
            self.today.day = ingest.datetime.date.fromisoformat(day)
            fr = self.go([art(1)], cfg)
            self.assertEqual((self.asked(), fr.recent), ([aid(1)], 1), day)
        self.today.day = ingest.datetime.date.fromisoformat("2026-10-17")  # 7 days later: rescanned
        fr = self.go([art(1)], cfg)
        self.assertEqual(self.asked(), [aid(1), aid(1)])
        self.assertEqual(self.f.uploaded, [sid(1), sid(2)])  # only the new track
        self.assertEqual((fr.counts["new"], fr.counts["exists"], fr.recent), (1, 1, 0))
        self.assertEqual(self.state().artists[aid(1)].isoformat(), "2026-10-17")

    def test_rescan_days_zero_scans_every_run(self):
        self.songs = {aid(1): [song(1)]}
        cfg = ingest.FollowedConfig(rescan_days=0)
        self.go([art(1)], cfg)
        self.go([art(1)], cfg)
        self.assertEqual(self.asked(), [aid(1), aid(1)])

    def test_a_scan_dated_in_the_future_counts_as_due(self):
        st = self.state()
        self.today.day = ingest.datetime.date(2027, 1, 1)
        st.record_artist(aid(1))
        self.today.day = ingest.datetime.date(2026, 10, 10)
        self.assertTrue(self.state().artist_due(aid(1), 7))

    def test_sign_in_mid_artist_stops_the_whole_run(self):
        self.songs = {aid(1): [song(1)], aid(2): [song(2), song(3), song(4)], aid(3): [song(5)]}
        deps = self.f.deps()
        inner = deps.download

        def download(s, m, t):
            if s["song_id"] == sid(3):
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
            return inner(s, m, t)

        deps.download = download
        fr = ingest.FollowedRun()
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(ingest.YouTubeSignInRequired):
            ingest.run_followed(fr, ingest.FollowedConfig(), [art(1), art(2), art(3)], self.enum, deps, self.existing,
                                {}, self.dir / "work", False, ingest.RunLimits(max_new=5), self.state())
        self.assertEqual(self.asked(), [aid(1), aid(2)])  # artist 3 is never enumerated
        self.assertEqual((fr.status, fr.counts["new"], fr.not_started, fr.not_attempted), ("interrupted", 2, 1, 2))
        self.assertEqual(self.f.uploaded, [sid(1), sid(2)])
        self.assertEqual(set(self.state().artists), {aid(1)})  # what was finished before stays recorded
        self.assertIn("interrupted by youtube-sign-in", out.getvalue())

    def test_dry_run_downloads_nothing_and_records_nothing(self):
        self.songs = {aid(1): [song(1), song(2)]}
        fr = self.go([art(1)], dry=True)
        self.assertEqual((self.f.uploaded, fr.counts["dry_run"], fr.scanned), ([], 2, 1))
        self.assertFalse(self.path.exists())

    def test_old_state_files_without_artists_load_and_the_playlist_file_is_unchanged(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": 1, "tracks": {}}))
        self.assertEqual(self.state().artists, {})
        st = self.state()
        st.record(sid(1), "no_match")
        self.assertEqual(set(json.loads(self.path.read_text())), {"version", "tracks"})  # no `artists` key yet
        st.record_artist(aid(1))
        self.assertEqual(json.loads(self.path.read_text())["artists"], {aid(1): "2026-10-10"})
        self.assertEqual(stat_mode(self.path), 0o600)

    def test_malformed_artist_entries_are_ignored_with_a_warning(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": 1, "tracks": {}, "artists": {
            aid(1): "2026-10-01", "short": "2026-10-01", aid(2): "not a date", aid(3): 5}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st = self.state()
        self.assertEqual(set(st.artists), {aid(1)})
        self.assertIn("ignored 3 malformed entries", out.getvalue())
        self.path.write_text(json.dumps({"version": 1, "tracks": {}, "artists": [1]}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.state().artists, {})


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


class FollowedMainTest(unittest.TestCase):
    """main() end to end: token, followed list and enumerator are fakes; the exit codes and the result JSON."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "work").mkdir()
        self.today = FixedToday()
        self.f = Fakes(self.dir)
        self.songs = {aid(1): [song(1), song(2)], aid(2): [song(3)]}
        self.followed = [art(1), art(2)]
        self.enum_calls, self.d1_calls, self.token_calls = [], [], []

    def run_main(self, sources, argv=(), login=True, token=None, list_error=None, playlist_songs=None, deps=None):
        (self.dir / "s.yml").write_text(sources)
        env = {k: "x" for k in ingest.REQUIRED_ENV}

        def enumerate_artist(artist, groups, market):
            self.enum_calls.append(artist)
            v = self.songs[artist]
            if isinstance(v, Exception):
                raise v
            return v

        def get_token():
            self.token_calls.append(1)
            if isinstance(token, Exception):
                raise token
            return "access-token-value"

        def fake_spotdl(args, cwd):
            Path(args[args.index("--save-file") + 1]).write_text(json.dumps(playlist_songs or []))
            return [], ""

        def list_followed(tok):
            if list_error:
                raise list_error
            return list(self.followed)

        def d1(sql, params=None):
            self.d1_calls.append(sql)
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
                mock.patch.object(ingest, "real_deps", lambda ytm, thr: deps or self.f.deps()), \
                mock.patch.object(ingest, "real_enumerator", lambda cid, secret: enumerate_artist), \
                mock.patch.object(spotify_user, "get_access_token", get_token), \
                mock.patch.object(spotify_user, "followed_artists", list_followed), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                contextlib.redirect_stdout(out):
            os.environ.pop("SPOTIFY_USER_REFRESH_TOKEN", None)  # restored by patch.dict
            if login:
                os.environ["SPOTIFY_USER_REFRESH_TOKEN"] = "r"
            code = ingest.main()
        self.out = out.getvalue()
        return code, json.loads(res.read_text()) if res.exists() else None

    SECTION = "followed_artists:\n"

    def test_artists_only_file_ingests_and_reports(self):
        code, r = self.run_main(self.SECTION)
        self.assertEqual((code, r["stop_reason"], r["failure_class"]), (0, "done", "none"))
        self.assertEqual(self.f.uploaded, [sid(1), sid(2), sid(3)])
        self.assertEqual(r["playlists"], [])
        self.assertEqual((r["counts"]["added"], r["followed_artists"]["added"], r["followed_artists"]["status"]),
                         (3, 3, "done"))
        self.assertEqual(r["followed_artists"]["artists"],
                         {"listed": 2, "excluded": 0, "skipped_recent": 0, "deferred": 0, "scanned": 2, "failed": 0,
                          "not_started": 0})
        self.assertIn("followed artists: 2 listed by Spotify", self.out)
        self.assertIn("done: 3 new", self.out)
        self.assertNotIn("access-token-value", self.out)
        self.assertEqual(set(json.loads((self.dir / "st.json").read_text())["artists"]), {aid(1), aid(2)})
        self.assertNotIn(aid(1), json.dumps(r))  # the result carries counts only

    def test_playlists_and_artists_share_the_cap(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{self.SECTION}", ["--max-new", "3"],
                                playlist_songs=[song(10), song(11)])
        self.assertEqual((code, r["stop_reason"]), (0, "cap"))
        self.assertEqual(self.f.uploaded, [sid(10), sid(11), sid(1)])
        self.assertEqual((r["playlists"][0]["added"], r["followed_artists"]["added"], r["counts"]["added"]), (2, 1, 3))
        self.assertEqual(r["followed_artists"]["status"], "stopped")
        self.assertEqual(r["followed_artists"]["not_attempted"], 1)

    def test_playlist_run_that_hit_the_cap_starts_no_artist(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{self.SECTION}", ["--max-new", "1"], playlist_songs=[song(10), song(11)])
        self.assertEqual((code, r["stop_reason"], self.enum_calls), (0, "cap", []))
        self.assertEqual(r["followed_artists"]["artists"]["not_started"], 2)

    def test_missing_user_login_is_a_config_error_with_exit_2_and_nothing_runs(self):
        code, r = self.run_main(f"- {GOOD_URL}\n{self.SECTION}", login=False)
        self.assertEqual(code, 2)
        self.assertEqual((r["failure_class"], r["exit_code"], r["stop_reason"]), ("config", 2, None))
        self.assertIn("SPOTIFY_USER_REFRESH_TOKEN is not set", self.out)
        self.assertIn("python ingest/spotify_user.py login", self.out)
        self.assertEqual((self.d1_calls, self.enum_calls, self.token_calls, self.f.uploaded), ([], [], [], []))

    def test_invalid_token_is_a_config_error_with_exit_2(self):
        err = spotify_user.UserAuthError("Spotify rejected the grant (invalid_grant): expired; run `python "
                                         "ingest/spotify_user.py login` again")
        code, r = self.run_main(self.SECTION, token=err)
        self.assertEqual((code, r["failure_class"]), (2, "config"))
        self.assertIn("invalid_grant", self.out)
        self.assertEqual((self.d1_calls, self.f.uploaded), ([], []))

    def test_a_failing_followed_list_is_a_config_error_too(self):
        code, r = self.run_main(self.SECTION, list_error=spotify_user.UserAuthError("Spotify refused the request (HTTP 403)"))
        self.assertEqual((code, r["failure_class"], self.d1_calls), (2, "config", []))
        self.assertIn("cannot read the followed artists: Spotify refused the request (HTTP 403)", self.out)

    def test_disabled_section_needs_no_login_and_runs_nothing_extra(self):
        code, r = self.run_main(f"- {GOOD_URL}\nfollowed_artists:\n  enabled: false\n", login=False,
                                playlist_songs=[song(10)])
        self.assertEqual((code, self.token_calls, self.enum_calls), (0, [], []))
        self.assertNotIn("followed_artists", r)

    def test_flat_playlist_file_never_touches_the_artist_code_or_the_login(self):
        code, r = self.run_main(f"- {GOOD_URL}\n", login=False, playlist_songs=[song(10)])
        self.assertEqual((code, self.token_calls, self.enum_calls), (0, [], []))
        self.assertEqual(r["stop_reason"], "done")
        self.assertNotIn("followed_artists", r)
        self.assertEqual(set(r), {"failure_class", "exit_code", "stop_reason", "playlists", "counts"})
        self.assertNotIn("artists", json.loads((self.dir / "st.json").read_text()) if (self.dir / "st.json").exists() else {})
        self.assertNotIn("followed artists", self.out)

    def test_a_failed_enumeration_gives_exit_1_and_the_others_ingest(self):
        self.songs[aid(1)] = artist_tracks.ArtistTracksError("Spotify request failed: HTTP 500")
        code, r = self.run_main(self.SECTION)
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (1, "partial", "done"))
        self.assertEqual(self.f.uploaded, [sid(3)])
        self.assertEqual((r["followed_artists"]["artists"]["failed"], r["followed_artists"]["artists"]["scanned"]), (1, 1))
        self.assertIn("1 artist(s) could not be enumerated", self.out)

    def test_sign_in_in_the_artist_phase_exits_3_and_keeps_the_playlists_result(self):
        deps = self.f.deps()
        inner = deps.download

        def download(s, m, t):
            if s["song_id"] == sid(2):
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
            return inner(s, m, t)

        deps.download = download
        code, r = self.run_main(f"- {GOOD_URL}\n{self.SECTION}", playlist_songs=[song(10)], deps=deps)
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (3, "youtube-sign-in", "sign-in"))
        self.assertEqual([p["status"] for p in r["playlists"]], ["done"])
        self.assertEqual(self.enum_calls, [aid(1)])  # the second artist is not enumerated
        self.assertEqual(r["followed_artists"]["status"], "interrupted")
        self.assertEqual(r["followed_artists"]["artists"]["not_started"], 1)
        self.assertEqual((r["counts"]["added"], r["playlists"][0]["added"]), (2, 1))  # playlist track + artist track 1

    def test_second_run_adds_only_new_tracks_and_skips_recent_artists(self):
        self.run_main(self.SECTION)
        before = list(self.f.uploaded)
        self.enum_calls.clear()
        code, r = self.run_main(self.SECTION)  # same day: everything is recent
        self.assertEqual((code, self.enum_calls, self.f.uploaded, r["followed_artists"]["artists"]["skipped_recent"]),
                         (0, [], before, 2))

    def test_dry_run_with_artists_writes_no_artist_state(self):
        code, r = self.run_main(self.SECTION, ["--dry-run"])
        self.assertEqual((code, self.f.uploaded, r["counts"]["added"]), (0, [], 0))
        self.assertIn("dry-run would ingest", self.out)
        self.assertFalse((self.dir / "st.json").exists())

    def test_no_playlist_scope_and_no_write_call(self):
        self.assertEqual(spotify_user.SCOPE, "user-follow-read")
        src = (REPO / "ingest" / "spotify_user.py").read_text() + (REPO / "ingest" / "artist_tracks.py").read_text()
        self.assertNotIn("playlist-modify", src)
        self.assertNotIn("method=", src)  # every request in the Spotify modules is a GET or the token POST


class RealEnumeratorTest(unittest.TestCase):
    def test_fresh_catalog_per_artist_with_the_configured_groups_and_market(self):
        seen = []

        def fake_tracks(cat, artist, groups, market, **kw):
            seen.append((cat, artist, groups, market, kw["out"]))
            return []

        with mock.patch.object(artist_tracks, "artist_tracks", fake_tracks):
            enum = ingest.real_enumerator("cid", "csecret")
            enum(aid(1), ("album",), "DE")
            enum(aid(2), ("single",), "US")
        self.assertEqual([(a, g, m) for _, a, g, m, _ in seen], [(aid(1), ("album",), "DE"), (aid(2), ("single",), "US")])
        self.assertIsNot(seen[0][0], seen[1][0])           # each artist gets its own API call budget
        self.assertEqual(seen[0][0].secrets, ["cid", "csecret"])
        self.assertIsNone(seen[0][4]("progress line"))     # per-album progress is not logged


class IncompleteReadTest(unittest.TestCase):
    """http.client.IncompleteRead is not an OSError: it must become the module's own error, without a traceback."""

    class Opener:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"partial", 10)

        status = 200
        headers = {}

    def test_spotify_user_default_get_and_post(self):
        build = mock.Mock(return_value=SimpleNamespace(open=lambda *a, **k: self.Opener()))
        with mock.patch.object(spotify_user.urllib.request, "build_opener", build), \
                self.assertRaisesRegex(spotify_user.UserAuthError, "network error talking to Spotify .IncompleteRead"):
            spotify_user.default_get("https://api.spotify.com/v1/me", {"Authorization": "Bearer tok"}, 1)
        with mock.patch.object(spotify_user.urllib.request, "urlopen", lambda *a, **k: self.Opener()), \
                self.assertRaisesRegex(spotify_user.UserAuthError, "IncompleteRead"):
            spotify_user.default_post("https://accounts.spotify.com/api/token", {"a": "b"}, 1)

    def test_artist_tracks_default_fetch_and_token(self):
        with mock.patch.object(artist_tracks, "_OPEN", lambda *a, **k: self.Opener()):
            with self.assertRaisesRegex(artist_tracks.ArtistTracksError, "IncompleteRead"):
                artist_tracks.default_fetch("https://api.spotify.com/v1/x", {"Authorization": "Bearer tok"}, 1)
            with self.assertRaisesRegex(artist_tracks.ArtistTracksError, "IncompleteRead"):
                artist_tracks.client_credentials_token("id", "secret")


if __name__ == "__main__":
    unittest.main()

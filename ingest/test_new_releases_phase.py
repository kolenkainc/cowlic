"""Tests for the nightly new-release phase of ingest.py (#172). Offline: a fake YouTube Music source, fake deps, fake D1,
a temp state directory, an injected clock for the 1 s pacing (NoNetwork makes a forgotten patch fail fast).

The names below are deliberately odd ("Zyxel ...") so that a leak of an artist or a title into the log is easy to see."""
import contextlib
import dataclasses
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bootstrap_ranking
import ingest
import lastfm
import new_releases
import tg_check
from match import normalize
from test_lastfm_source import NoNetwork

URL = "https://open.spotify.com/playlist/" + "a" * 22
SECTION = "new_releases:\n  enabled: true\n"
TODAY = ingest.utc_today()


def day(offset: int) -> str:
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def artist_names(n: int) -> list[str]:
    return [f"Zyxel Quartet {i:02d}" for i in range(n)]


def tracks_of(i: int) -> list[str]:
    return [f"Zyxel Song {i:02d}a", f"Zyxel Song {i:02d}b"]


class FakeYTM:
    """The four YouTube Music calls. Artist i (in `names` order) has one missing release with two tracks, unless
    `releases[name]` says otherwise. Every call is logged to `log` (shared with the deps, to prove the order)."""

    def __init__(self, names, log, releases=None, fail=None):
        self.names, self.log, self.releases, self.fail = list(names), log, releases or {}, fail or {}
        self.calls: list[str] = []

    def _go(self, name):
        self.calls.append(name)
        self.log.append(("source", name))
        if name in self.fail:
            raise self.fail[name]

    def _index(self, bid):
        return int(bid[2:])

    def search(self, query, filter=None, limit=20):
        self._go("search")
        i = self.names.index(query)
        return [{"resultType": "artist", "artist": query, "browseId": f"UC{i}"}]

    def get_artist(self, channelId):
        self._go("get_artist")
        return {"albums": {"browseId": channelId, "params": "PA", "results": []},
                "singles": {"browseId": channelId, "params": "PS", "results": []}}

    def get_artist_albums(self, channelId, params, limit=100, order=None):
        self._go("get_artist_albums")
        i = self._index(channelId)
        if params != "PA":
            return []
        return self.releases.get(self.names[i], [{"title": f"Zyxel Album {i:02d}", "year": "2026",
                                                    "browseId": f"MPRE{i}", "type": "Album"}])

    def get_album(self, browseId):
        self._go("get_album")
        i = int(browseId[4:])
        return {"title": f"Zyxel Album {i:02d}", "year": "2026",
                "tracks": [{"title": t, "artists": [{"name": self.names[i]}], "duration_seconds": 200 + k,
                            "trackNumber": k + 1} for k, t in enumerate(tracks_of(i))]}


class Match:
    video_id, relaxed, mode, duration_s = "vid", False, "x", 200


class Base(NoNetwork):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state_file = self.dir / "st.json"
        self.nr_state = self.dir / new_releases.STATE_FILE_NAME
        self.log: list = []          # ordered events of the whole run: playlist deps and source calls
        self.d1: list[str] = []      # every SQL text the fake D1 saw
        self.uploaded: list[str] = []
        self.sleeps: list[float] = []
        self.stored_albums: list[dict] = []
        self.deps_fail: dict = {}    # song name prefix -> exception raised by download
        clock = [1000.0]

        def sleep(s):
            self.sleeps.append(s)
            clock[0] += s
        pacer = new_releases.Pacer(1.0, lambda: clock[0], sleep)
        p = mock.patch.object(new_releases, "_DEFAULT_PACER", pacer)
        p.start()
        self.addCleanup(p.stop)

    # ---- fixtures ----
    def write_ranking(self, names):
        doc = bootstrap_ranking.build([{"name": n, "mbid": None, "playcount": 1000 - i} for i, n in enumerate(names)],
                                      5, 1000)
        bootstrap_ranking.save(self.dir / bootstrap_ranking.FILE_NAME, doc)

    def write_nr_state(self, artists: dict):
        new_releases.save_state(self.nr_state, {"version": 1, "artists": {
            new_releases.artist_key(n): e for n, e in artists.items()}})

    def deps(self):
        def find(song):
            self.log.append(("find", song["song_id"]))
            return Match()

        def download(song, m, tdir):
            self.log.append(("download", song["song_id"]))
            for prefix, exc in self.deps_fail.items():
                if song["name"].startswith(prefix):
                    raise exc
            p = tdir / "a.mp3"
            p.write_bytes(b"x" * 10)
            return p

        def upload(song, path):
            self.uploaded.append(song["name"])
            return {"audio": {"file_id": f"f{len(self.uploaded)}"}, "message_id": len(self.uploaded)}

        return ingest.Deps(find, download, upload, lambda s, t: "cov", lambda stmts: [], lambda i: None)

    def run_main(self, sources, argv=(), ranking="default", names=None, playlist=(), releases=None, source_fail=None,
                 stored=(), stdout_extra=None):
        """Runs ingest.main(); returns (exit code, result dict or None, stdout). `names` is the artist order the
        fake source knows (default: the ranking)."""
        names = names if names is not None else artist_names(15)
        if ranking == "default":
            ranking = names
        if ranking is not None:
            self.write_ranking(ranking)
        self.ytm = FakeYTM(names, self.log, releases, source_fail)
        (self.dir / "s.yml").write_text(sources)

        def d1(sql, params=None):
            self.d1.append(sql)
            if sql == ingest.STORED_ROWS_SQL:
                return [{"title": t, "artist": a, "duration_s": d} for t, a, d in stored]
            if sql == ingest.STORED_ALBUMS_SQL:
                return self.stored_albums
            return []

        def spotdl(args, cwd):
            self.log.append(("playlist", args[1]))
            Path(args[args.index("--save-file") + 1]).write_text(json.dumps(list(playlist)))
            return [], ""

        res = self.dir / "r.json"
        full = ["ingest.py", "--sources", str(self.dir / "s.yml"), "--result-json", str(res),
                "--state-file", str(self.state_file), "--pace-seconds", "0", "--pace-jitter-seconds", "0", *argv]
        out = io.StringIO()
        env = {k: "x" for k in ingest.REQUIRED_ENV}
        with mock.patch.dict(os.environ, env), mock.patch.object(tg_check, "load_env"), \
                mock.patch.object(sys, "argv", full), mock.patch.object(ingest, "d1_query", d1), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "spotdl_run", spotdl), \
                mock.patch.object(ingest, "real_deps", lambda ytm, thr: self.deps()), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: self.ytm)}), \
                contextlib.redirect_stdout(out):
            code = ingest.main()
        self.out = out.getvalue()
        return code, (json.loads(res.read_text()) if res.exists() else None), self.out

    def nr_entries(self):
        return new_releases.load_state(self.nr_state)["artists"]

    def entry(self, name):
        return self.nr_entries().get(new_releases.artist_key(name))


def song(n, name=None):
    return {"song_id": "P" + str(n).zfill(21), "name": name or f"Playlist Song {n}", "artists": ["Pl"],
            "album_artist": "Pl", "album_name": "PA", "year": 2020, "track_number": n, "disc_number": 1,
            "duration": 200, "url": "https://open.spotify.com/track/" + "x" * 22, "cover_url": None}


class Caps(Base):
    def test_default_caps_ten_artists_then_the_rest_next_night(self):
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}")
        nrr = r["new_releases"]
        self.assertEqual((code, nrr["due"], nrr["checked"], nrr["with_new"], nrr["added"], nrr["failed"],
                          nrr["not_started"], nrr["status"]), (0, 10, 10, 10, 20, 0, 0, "done"))
        self.assertEqual(self.ytm.calls.count("search"), 10)  # the 11th..15th ranked artist waits
        self.assertEqual(len(self.uploaded), 20)               # the cap of tracks per night is exactly met
        self.assertEqual(r["counts"]["added"], 20)
        self.assertEqual(len(self.nr_entries()), 10)
        # same day again: the ten are not due, the other five are checked and add their tracks (resume)
        self.ytm.calls.clear()
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}")
        self.assertEqual((r["new_releases"]["due"], r["new_releases"]["checked"], r["new_releases"]["added"]), (5, 5, 10))
        self.assertEqual(len(self.uploaded), 30)

    def test_artists_are_taken_in_ranking_order(self):
        self.run_main(f"- {URL}\n{SECTION.replace('true', 'true')}  max_artists: 2\n")
        self.assertEqual(set(self.nr_entries()), {new_releases.artist_key(n) for n in artist_names(2)})

    def test_max_new_cuts_the_artist_in_progress_which_stays_due(self):
        code, r, out = self.run_main(f"- {URL}\n{SECTION}  max_new: 3\n")
        nrr = r["new_releases"]
        self.assertEqual((nrr["added"], nrr["status"], nrr["checked"]), (3, "stopped", 2))
        self.assertEqual(nrr["not_started"], 8)
        self.assertEqual(r["stop_reason"], "done")  # the phase's own cap is not a stop of the run
        self.assertEqual(len(self.uploaded), 3)
        # artist 0 is done and evaluated; artist 1 was cut: not evaluated, checked_at put back (due again)
        self.assertEqual(len(self.entry(artist_names(15)[0])["evaluated"]), 1)
        cut = self.entry(artist_names(15)[1])
        self.assertNotIn("checked_at", cut)
        self.assertEqual(cut.get("evaluated", []), [])
        # the next night checks the cut artist again (first, it is the oldest due one) and goes on with the others.
        # Limit (#238): this fake D1 forgets uploads; with a real one a release of which only some tracks landed is
        # "in library" afterwards and its remaining tracks are NOT retried.
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}  max_new: 3\n")
        self.assertEqual(self.entry(artist_names(15)[1])["checked_at"], day(0))
        self.assertEqual(r["new_releases"]["checked"], 2)  # artist 1 again (2 tracks), then artist 2 (cut at 3)

    def test_shared_max_new_of_a_heavy_playlist_night_leaves_the_artists_due(self):
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", ["--max-new", "1"], playlist=[song(1), song(2), song(3)])
        nrr = r["new_releases"]
        self.assertEqual((nrr["due"], nrr["checked"], nrr["not_started"], nrr["status"]), (10, 0, 10, "stopped"))
        self.assertEqual((r["stop_reason"], self.ytm.calls), ("cap", []))
        self.assertFalse(self.nr_state.exists())  # nothing was checked: the artists stay due
        self.assertNotIn(ingest.STORED_ALBUMS_SQL, self.d1)  # no D1 read without budget
        self.assertIn("stopped: new releases", out)

    def test_run_wide_cap_reached_inside_the_phase_is_the_stop_reason_of_the_run(self):
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}", ["--max-new", "5"])
        self.assertEqual((r["stop_reason"], len(self.uploaded), r["new_releases"]["status"]), ("cap", 5, "stopped"))
        self.assertEqual(r["counts"]["added"], 5)

    def test_recheck_days(self):
        names = artist_names(3)
        self.write_nr_state({names[0]: {"checked_at": day(-6)}, names[1]: {"checked_at": day(-7)},
                             names[2]: {"checked_at": day(-3)}})
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}", names=names, ranking=names)
        self.assertEqual(r["new_releases"]["due"], 1)  # only the 7-day-old check is due
        self.ytm.calls.clear()
        self.write_nr_state({names[0]: {"checked_at": day(-6)}, names[1]: {"checked_at": day(-7)},
                             names[2]: {"checked_at": day(-3)}})
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}  recheck_days: 3\n", names=names, ranking=names)
        self.assertEqual((r["new_releases"]["due"], r["new_releases"]["checked"]), (3, 3))  # the detector agrees

    def test_time_limit_of_the_phase_and_of_the_run(self):
        t = [0.0]
        parent = ingest.RunLimits(None, None, clock=lambda: t[0])
        pl = ingest.PhaseLimits(parent, 5, ingest.NEW_RELEASES_PHASE_MINUTES)
        self.assertEqual(ingest.NEW_RELEASES_PHASE_MINUTES, 20)
        self.assertIsNone(pl.exhausted())
        t[0] = 20 * 60 - 1
        self.assertIsNone(pl.exhausted())
        t[0] = 20 * 60
        self.assertEqual(pl.exhausted(), "time")
        pl.stop("time")
        self.assertIsNone(parent.stop_reason)  # the phase's own clock is not a stop of the run
        # the run's limit is passed on
        parent2 = ingest.RunLimits(None, 1, clock=lambda: t[0] - 20 * 60)
        pl2 = ingest.PhaseLimits(parent2, 5, 20)
        t[0] = 20 * 60 + 61
        self.assertEqual(pl2.exhausted(), "time")
        pl2.stop("time")
        self.assertEqual(parent2.stop_reason, "time")
        # attempts count for both
        pl3 = ingest.PhaseLimits(ingest.RunLimits(2), 1, 20)
        pl3.attempted += 1
        self.assertEqual((pl3.parent.attempted, pl3.exhausted()), (1, "cap"))


class RequestBudget(Base):
    def test_first_night_and_second_night_requests_and_pacing(self):
        names = artist_names(1)
        self.run_main(f"- {URL}\n{SECTION}", names=names)
        # search + get_artist + get_artist_albums (albums, singles) + get_album of the one missing release
        self.assertEqual(self.ytm.calls, ["search", "get_artist", "get_artist_albums", "get_artist_albums", "get_album"])
        self.assertEqual(self.sleeps, [1.0] * 4)  # at least 1 s between two source calls
        self.ytm.calls.clear()
        self.write_nr_state({names[0]: {"browse_id": "UC0", "checked_at": day(-7), "evaluated": []}})
        self.stored_albums = [{"artist": names[0], "album": "Zyxel Album 00", "year": 2026}]
        self.run_main(f"- {URL}\n{SECTION}", names=names)
        # the second night: no search (browse id cached), the release is in the library: no get_album
        self.assertEqual(self.ytm.calls, ["get_artist", "get_artist_albums", "get_artist_albums"])

    def test_one_extra_read_only_parameterless_album_query_once_per_run(self):
        self.run_main(f"- {URL}\n{SECTION}")
        self.assertEqual(self.d1.count(ingest.STORED_ALBUMS_SQL), 1)
        self.assertEqual(self.d1.count(ingest.STORED_ROWS_SQL), 1)
        self.assertTrue(ingest.STORED_ALBUMS_SQL.startswith("SELECT ") and "?" not in ingest.STORED_ALBUMS_SQL)

    def test_stored_album_of_the_artist_is_filtered_in_python(self):
        names = artist_names(2)
        self.stored_albums = [{"artist": names[0].upper(), "album": "Zyxel Album 00", "year": 2026},
                              {"artist": "Somebody Else", "album": "Zyxel Album 01", "year": 2026}]
        code, r, _ = self.run_main(f"- {URL}\n{SECTION}", names=names)
        self.assertEqual((r["new_releases"]["checked"], r["new_releases"]["with_new"], r["new_releases"]["added"]),
                         (2, 1, 2))  # artist 0 has its album, artist 1 does not (the other artist's album is not its)

    def test_source_refusal_ends_the_phase_without_retry(self):
        err = type("YTMusicServerError", (Exception,), {})("HTTP 429 Too Many Requests")
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", source_fail={"search": err}, playlist=[song(1)])
        nrr = r["new_releases"]
        self.assertEqual((nrr["failed"], nrr["checked"], nrr["not_started"], nrr["status"]), (1, 0, 9, "stopped"))
        self.assertEqual(self.ytm.calls, ["search"])  # one request, no retry, no further artist
        self.assertEqual((code, r["failure_class"]), (1, "partial"))
        self.assertEqual(self.uploaded, ["Playlist Song 1"])  # the playlist phase is untouched
        self.assertEqual(self.entry(artist_names(15)[0])["failures"], 1)  # backs off in the detector's state

    def test_one_failing_artist_does_not_stop_the_others(self):
        err = RuntimeError("boom")
        names = artist_names(3)
        calls = []
        orig = FakeYTM.get_artist

        def get_artist(self, cid):
            calls.append(cid)
            if cid == "UC0":
                raise err
            return orig(self, cid)
        with mock.patch.object(FakeYTM, "get_artist", get_artist):
            code, r, out = self.run_main(f"- {URL}\n{SECTION}", names=names)
        nrr = r["new_releases"]
        self.assertEqual((nrr["failed"], nrr["checked"], nrr["status"], code), (1, 2, "done", 1))
        self.assertNotIn("boom", out)


class Order(Base):
    def test_phase_runs_after_the_playlists(self):
        self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1), song(2)], names=artist_names(2))
        kinds = [e[0] for e in self.log]
        last_playlist_work = max(i for i, k in enumerate(kinds) if k in ("playlist", "find", "download")
                                 and (k == "playlist" or self.log[i][1].startswith("P")))
        first_source = kinds.index("source")
        self.assertLess(last_playlist_work, first_source)

    def test_a_failed_phase_does_not_stop_or_fail_the_playlists(self):
        self.nr_state.write_text("{not json")
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1), song(2)])
        self.assertEqual(self.uploaded, ["Playlist Song 1", "Playlist Song 2"])
        self.assertEqual((code, r["failure_class"], r["new_releases"]["status"]), (1, "partial", "failed"))
        self.assertEqual(self.nr_state.read_text(), "{not json")  # never replaced silently
        self.assertEqual(self.ytm.calls, [])
        self.assertEqual(r["playlists"][0]["added"], 2)

    def test_d1_failure_reading_the_library_fails_the_phase_only(self):
        with mock.patch.object(ingest, "read_stored_albums", side_effect=ingest.IngestError("D1 API error 500")):
            code, r, out = self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1)])
        self.assertEqual((code, r["new_releases"]["status"], self.uploaded), (1, "failed", ["Playlist Song 1"]))
        self.assertEqual(self.ytm.calls, [])

    def test_track_failure_in_the_phase_is_a_failed_track_class_partial(self):
        self.deps_fail = {"Zyxel Song 00a": ingest.IngestError("expected exactly one mp3")}
        code, r, out = self.run_main(f"- {URL}\n{SECTION}  max_artists: 1\n", names=artist_names(1))
        self.assertEqual((code, r["failure_class"], r["counts"]["failed"], r["counts"]["added"]), (1, "partial", 1, 1))
        # the release has a failed track: not evaluated, retried later
        self.assertEqual(self.entry(artist_names(1)[0])["evaluated"], [])

    def test_unexpected_exception_in_the_phase_tracks_ends_the_phase_not_the_run(self):
        boom = type("YTMusicServerError", (Exception,), {})("secret Zyxel Quartet 00 text")

        def find(song):
            if song["name"].startswith("Zyxel Song 00a"):
                raise boom
            return Match()
        base = self.deps
        self.deps = lambda: dataclasses.replace(base(), find_match=find)
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1)])
        self.assertEqual((code, r["failure_class"], r["new_releases"]["status"]), (1, "partial", "failed"))
        self.assertEqual((r["new_releases"]["failed"], r["new_releases"]["not_started"]), (1, 9))
        self.assertEqual(self.uploaded, ["Playlist Song 1"])
        self.assertIn("(YTMusicServerError)", out)
        self.assertNotIn("Zyxel", out)
        self.assertIn("new-release check(s) failed", out)
        self.assertNotIn("could not be enumerated", out)
        self.assertEqual(self.ytm.calls.count("search"), 1)  # the phase ended
        self.assertTrue(self.nr_state.exists())  # state was saved

    def test_never_computes_the_lastfm_ranking_and_reads_the_stored_one_only(self):
        self.write_ranking(artist_names(2))
        boom = AssertionError("the nightly must never call Last.fm")
        with mock.patch.object(lastfm, "top_artists", side_effect=boom), \
                mock.patch.object(lastfm, "LastfmClient", side_effect=boom), \
                mock.patch.object(bootstrap_ranking, "compute", side_effect=boom), \
                mock.patch.object(bootstrap_ranking, "save", side_effect=boom), \
                mock.patch.object(bootstrap_ranking, "read", wraps=bootstrap_ranking.read) as read:
            code, r, _ = self.run_main(f"- {URL}\n{SECTION}", ranking=None, names=artist_names(2))
        self.assertEqual((code, r["new_releases"]["checked"]), (0, 2))
        read.assert_called_once_with(self.dir / bootstrap_ranking.FILE_NAME)

    def test_the_bootstrap_files_are_not_touched(self):
        self.run_main(f"- {URL}\n{SECTION}", names=artist_names(2))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("bootstrap")),
                         [bootstrap_ranking.FILE_NAME])


class NothingToDo(Base):
    def test_disabled_section_does_nothing_and_adds_nothing(self):
        code, r, out = self.run_main(f"- {URL}\nnew_releases:\n  enabled: false\n", playlist=[song(1)])
        self.assertEqual((code, self.ytm.calls, self.uploaded), (0, [], ["Playlist Song 1"]))
        self.assertNotIn("new_releases", r)
        self.assertNotIn("new releases", out)
        self.assertNotIn(ingest.STORED_ALBUMS_SQL, self.d1)
        self.assertFalse(self.nr_state.exists())

    def test_absent_section_does_nothing(self):
        code, r, out = self.run_main(f"- {URL}\n", playlist=[song(1)])
        self.assertEqual((code, self.ytm.calls), (0, []))
        self.assertNotIn("new_releases", r)
        self.assertNotIn("new releases", out)

    def test_no_stored_ranking_one_line_and_nothing_done(self):  # no D1 artist-set fallback (not implemented, see PR)
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", ranking=None, playlist=[song(1)])
        self.assertEqual((code, self.ytm.calls, self.uploaded), (0, [], ["Playlist Song 1"]))
        self.assertEqual([ln for ln in out.splitlines() if "new releases" in ln],
                         ["new releases: no usable stored ranking (no ranking file); nothing to do "
                          "(run the bootstrap command first)"])
        self.assertEqual(r["new_releases"], {"status": "no_ranking", "due": 0, "checked": 0, "with_new": 0, "added": 0,
                                             "skipped": 0, "failed": 0, "not_started": 0})
        self.assertNotIn(ingest.STORED_ALBUMS_SQL, self.d1)  # no D1 artist-set fallback: the ranking or nothing
        self.assertFalse(self.nr_state.exists())

    def test_corrupt_ranking_is_no_ranking(self):
        (self.dir / bootstrap_ranking.FILE_NAME).write_text("{")
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", ranking=None)
        self.assertEqual((code, r["new_releases"]["status"], self.ytm.calls), (0, "no_ranking", []))

    def test_nothing_due_reads_nothing_from_d1(self):
        names = artist_names(2)
        self.write_nr_state({n: {"checked_at": day(-1)} for n in names})
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", names=names)
        self.assertEqual((code, r["new_releases"]["due"], self.ytm.calls), (0, 0, []))
        self.assertNotIn(ingest.STORED_ALBUMS_SQL, self.d1)
        self.assertIn("new releases: 0 due", out)


class SignIn(Base):
    def test_sign_in_challenge_in_the_phase_stops_everything_with_exit_3(self):
        self.deps_fail = {"Zyxel Song 01a": ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")}
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1)])
        self.assertEqual((code, r["failure_class"], r["stop_reason"]), (3, "youtube-sign-in", "sign-in"))
        nrr = r["new_releases"]
        self.assertEqual((nrr["status"], nrr["checked"], nrr["not_started"]), ("interrupted", 2, 8))
        self.assertEqual(self.ytm.calls.count("search"), 2)  # artist 2 onwards were never looked at
        self.assertEqual(self.uploaded, ["Playlist Song 1", "Zyxel Song 00a", "Zyxel Song 00b"])
        # the challenged artist stays due and its release is not evaluated; the finished one is
        self.assertEqual(len(self.entry(artist_names(15)[0])["evaluated"]), 1)
        self.assertNotIn("checked_at", self.entry(artist_names(15)[1]))
        self.assertIn("exit code 3", out)
        for forbidden in ("cookie", "proxy"):
            self.assertNotIn(forbidden, out.replace("do not add cookies", ""))

    def test_sign_in_in_the_playlist_means_the_phase_never_runs(self):
        self.deps_fail = {"Playlist Song 1": ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")}
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", playlist=[song(1)])
        self.assertEqual((code, self.ytm.calls), (3, []))
        self.assertNotIn("new_releases", r)


class DryRun(Base):
    def test_dry_run_writes_no_state_and_adds_nothing(self):
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", ["--dry-run"], names=artist_names(2))
        self.assertEqual((code, self.uploaded, r["counts"]["added"]), (0, [], 0))
        self.assertFalse(self.nr_state.exists())
        self.assertFalse(self.state_file.exists())
        self.assertEqual(r["new_releases"]["checked"], 2)  # the read-only checks ran
        self.assertEqual(r["new_releases"]["added"], 0)


class NoNamesInTheShippedLog(Base):
    def test_no_artist_release_or_track_name_in_stdout_or_result(self):
        self.deps_fail = {"Zyxel Song 01a": ingest.IngestError("spotdl output: Zyxel Quartet 01 - Zyxel Song 01a")}
        self.stored_albums = [{"artist": "Zyxel Quartet 02", "album": "Zyxel Album 02", "year": 2026}]
        code, r, out = self.run_main(f"- {URL}\n{SECTION}  max_artists: 4\n", names=artist_names(4))
        blob = out + json.dumps(r)
        self.assertIn("new releases lfm:", out)  # hashed keys are there
        self.assertIn("ERROR lfm:", out)         # a failed track is named by its song id and its error class
        for text in ("Zyxel", "Quartet", "spotdl output"):
            self.assertNotIn(text, blob)
        for i in range(4):
            for t in tracks_of(i):
                self.assertNotIn(t, blob)
        key = new_releases.artist_key(artist_names(4)[0])
        self.assertIn(key, out)

    def test_state_files_hold_hashes_only(self):
        self.run_main(f"- {URL}\n{SECTION}", names=artist_names(2))
        self.assertNotIn("Zyxel", self.nr_state.read_text())
        self.assertNotIn("Zyxel", self.state_file.read_text() if self.state_file.exists() else "")

    def test_the_artist_name_log_lines_of_the_lastfm_engine_are_not_reused(self):
        code, r, out = self.run_main(f"- {URL}\n{SECTION}", names=artist_names(2))
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("artist ")])


class Quiet(NoNetwork):
    def test_run_quiet_names_tracks_by_id_and_reports_outcomes(self):
        out = io.StringIO()
        d = Base.deps(SimpleNamespace(log=[], uploaded=[], deps_fail={}))
        s = {"song_id": "lfm:0123456789abcdef", "name": "Zyxel Song", "artists": ["Zyxel Band"], "album_artist": "Zyxel Band",
             "album_name": "Al", "year": 2026, "track_number": 1, "disc_number": 1, "duration": 200, "url": None,
             "cover_url": None}
        outcomes: dict = {}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            counts = ingest.run([s], d, set(), {}, Path(tmp), False, outcomes=outcomes, quiet=True)
        self.assertEqual((counts["new"], outcomes), (1, {s["song_id"]: "new"}))
        self.assertIn(s["song_id"], out.getvalue())
        self.assertNotIn("Zyxel", out.getvalue())

    def test_run_default_still_names_artist_and_title(self):
        out = io.StringIO()
        d = Base.deps(SimpleNamespace(log=[], uploaded=[], deps_fail={}))
        s = {"song_id": "lfm:0123456789abcdef", "name": "Zyxel Song", "artists": ["Zyxel Band"], "album_artist": "Zyxel Band",
             "album_name": "Al", "year": 2026, "track_number": 1, "disc_number": 1, "duration": 200, "url": None,
             "cover_url": None}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            ingest.run([s], d, set(), {}, Path(tmp), False)
        self.assertIn("ingested: Zyxel Band - Zyxel Song", out.getvalue())


class ResultShape(Base):
    def test_object_has_exactly_the_counts_and_no_ids(self):
        code, r, out = self.run_main(f"- {URL}\n{SECTION}  max_artists: 2\n", names=artist_names(2))
        self.assertEqual(set(r["new_releases"]), {"status", "due", "checked", "with_new", "added", "skipped", "failed",
                                                  "not_started"})
        self.assertTrue(all(isinstance(v, (int, str)) for v in r["new_releases"].values()))
        self.assertEqual(r["counts"]["added"], 4)

    def test_unresolvable_artist_is_skipped_not_failed(self):
        names = artist_names(2)
        with mock.patch.object(FakeYTM, "search", lambda self, q, filter=None, limit=20: [
                {"resultType": "artist", "artist": "Somebody Else", "browseId": "UCx"}]):
            code, r, out = self.run_main(f"- {URL}\n{SECTION}", names=names)
        self.assertEqual((code, r["new_releases"]["skipped"], r["new_releases"]["failed"]), (0, 2, 0))


class Config(unittest.TestCase):
    def parse(self, text):
        return ingest.parse_source_config(f"- {URL}\n{text}")

    def bad(self, text, pattern):
        with self.assertRaisesRegex(ingest.IngestError, pattern):
            self.parse(text)

    def test_defaults_are_the_accepted_caps(self):
        cfg = self.parse("new_releases:\n  enabled: true\n").new_releases
        self.assertEqual(cfg, ingest.NewReleasesConfig(True, 10, 7, 20))

    def test_keys(self):
        cfg = self.parse("new_releases:\n  max_artists: 3\n  recheck_days: 14\n  max_new: 5\n  enabled: false\n").new_releases
        self.assertEqual(cfg, ingest.NewReleasesConfig(False, 3, 14, 5))

    def test_unknown_and_repeated_keys_and_sections_fail(self):
        self.bad("new_releases:\n  lookback_days: 7\n", "unknown key 'lookback_days'")
        self.bad("new_releases:\n  max_new: 5\n  max_new: 6\n", "'max_new' given twice")
        self.bad("new_releases:\n  enabled: true\nnew_releases:\n  enabled: true\n", "given twice")
        self.bad("new_releases:\n  exclude:\n", "unknown key 'exclude'")
        self.bad("new_releases:\n  max_artists: 0\n", "max_artists must be a whole number from 1 to")
        self.bad("new_releases:\n  recheck_days: x\n", "recheck_days must be")
        self.bad("new_releases:\n  enabled: yes\n", "enabled must be true or false")
        self.bad("new_releases:\n    - something\n", "takes no list")
        self.bad("new_releases:\n   max_new: 5\n", "expected '  <key>: <value>'")
        self.bad("new_releases:\n  max_new:\n", "has no value")

    def test_a_section_alone_is_not_a_source(self):
        with self.assertRaisesRegex(ingest.IngestError, "lists no playlists"):
            ingest.parse_source_config("new_releases:\n  enabled: true\n")

    def test_absent_section_is_none(self):
        self.assertIsNone(self.parse("").new_releases)

    def test_lookback_days_fails_the_run_with_class_config_exit_1(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        src, res = Path(d.name) / "s.yml", Path(d.name) / "r.json"
        src.write_text(f"- {URL}\nnew_releases:\n  lookback_days: 7\n")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["ingest.py", "--sources", str(src), "--result-json", str(res)]), \
                contextlib.redirect_stdout(out):
            code = ingest.main()
        self.assertEqual((code, json.loads(res.read_text())["failure_class"]), (1, "config"))

    def test_the_repo_sources_yml_has_no_enabled_new_releases_section(self):
        text = (Path(__file__).resolve().parent.parent / "sources.yml").read_text(encoding="utf-8")
        cfg = ingest.parse_source_config(text).new_releases
        self.assertTrue(cfg is None or not cfg.enabled)
        self.assertNotIn("lastfm_artists:", text)  # the nightly wrapper refuses that key (PR #232)

    def test_no_lookback_days_anywhere_in_the_phase_code(self):
        src = (Path(__file__).resolve().parent / "ingest.py").read_text(encoding="utf-8")
        start = src.index("# ---- nightly new-release phase (#172)")
        self.assertNotIn("lookback", src[start:src.index("# ---- structured result")])


class StaleMessages(NoNetwork):
    """The two config-error hints of the Last.fm source no longer advise `enabled: false` as the way out: the nightly
    wrapper refuses the lastfm_artists key (exit 78) whatever `enabled` says; the ranking run is the bootstrap command."""

    def check(self, line):
        self.assertIn("remove or comment out the lastfm_artists section", line)
        self.assertIn("for a manual ingest.py run `enabled: false` also works", line)
        self.assertIn("bootstrap command, docs/bootstrap-ongoing.md", line)
        self.assertNotIn("or set `enabled: false` in the lastfm_artists section", line)
        self.assertIn(f"exit code {ingest.EXIT_CONFIG}", line)

    def test_prepare_lastfm_hint(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(out):
            os.environ.pop("LASTFM_API_KEY", None)
            client, code = ingest.prepare_lastfm(ingest.LastfmConfig(user="some_user"), Path("sources.yml"))
        self.assertEqual((client, code), (None, ingest.EXIT_CONFIG))
        self.check(out.getvalue())

    def test_ranking_refused_hint(self):
        err = lastfm.LastfmError("User not found")
        err.code = 6  # a refusal only the owner can fix
        out = io.StringIO()
        lr = ingest.LastfmRun()
        with mock.patch.object(lastfm, "top_artists", side_effect=err), contextlib.redirect_stdout(out):
            ingest.run_lastfm_phase(lr, ingest.LastfmConfig(user="some_user"), None, None, set(), {}, Path("."), False,
                                    ingest.RunLimits(), ingest.RunState())
        self.assertTrue(lr.config_error)
        self.check(out.getvalue())


if __name__ == "__main__":
    unittest.main()

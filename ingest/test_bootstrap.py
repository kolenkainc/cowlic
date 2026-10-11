"""Tests for bootstrap.py (#168). Offline: fake Last.fm, matcher, downloader, D1 and Telegram (a Backends object),
a temp state directory per test, no .env read, no network (NoNetwork base)."""
import contextlib
import datetime
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bootstrap
import bootstrap_ranking
import ingest
import lastfm
import lastfm_tracks
import tg_check
from test_ingest import Fakes, FixedToday
from test_lastfm_source import KEY, NoNetwork, lf_deps, lrec, rank

USER = "secretuser_x"  # a Last.fm user name that must appear nowhere but in the config file
NOW_LOCAL = datetime.datetime(2026, 10, 11, 12, 0, 0)  # far from 21:10
NAMES = ["Band 1", "Band 2", "Band 3"]


def at(records, status="ok", truncated=False):
    return lastfm_tracks.ArtistTracks(records=records, status=status, albums_truncated=truncated)


class Boom(BaseException):
    """Stands for a process that dies in the middle of a track (not an Exception: nothing catches it)."""


class FakeLastfm:
    """Serves the weekly charts of one recent week for `rank`; counts calls."""

    def __init__(self, artists=(("Band 1", 30), ("Band 2", 20), ("Band 3", 10)), fail=False):
        self.artists, self.fail, self.calls = artists, fail, 0

    def call(self, method, **params):
        self.calls += 1
        if self.fail:
            raise lastfm.LastfmError("boom")
        if method == "user.getWeeklyChartList":
            start = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).timestamp())
            return {"weeklychartlist": {"chart": [{"from": str(start), "to": str(start + 604800)}]}}
        return {"weeklyartistchart": {"artist": [{"name": n, "mbid": "", "playcount": str(c)} for n, c in self.artists]}}


class Base(NoNetwork):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "state" / "cowlic"
        env = {"XDG_STATE_HOME": str(self.root / "state"), "XDG_CONFIG_HOME": str(self.root / "config"),
               "LASTFM_API_KEY": KEY, **{k: "x" for k in ingest.REQUIRED_ENV}}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("COWLIC_LOCK_HELD", None)
        for target, name, value in ((tg_check, "load_env", lambda: None),
                                    (bootstrap, "local_now", lambda: NOW_LOCAL),
                                    (bootstrap, "today", FixedToday("2026-10-11")),
                                    (ingest, "js_runtime_problem", lambda: None),
                                    (bootstrap, "real_backends", self.backends),
                                    (bootstrap, "rank_client", self.rank_client)):
            q = mock.patch.object(target, name, value)
            q.start()
            self.addCleanup(q.stop)
        (self.root / "work").mkdir()
        self.f = Fakes(self.root)
        self.deps = lf_deps(self.f)
        self.existing: set = set()  # "D1": persists across the sessions of one test
        self.records: dict = {}
        self.calls: list = []
        self.backend_calls = 0
        self.planned_sizes: list = []
        self.fake_lastfm = FakeLastfm()
        self.rank_client_years: list = []
        self.out = self.err = ""

    # --- fakes ---

    def backends(self, planned):
        self.backend_calls += 1
        self.planned_sizes.append(planned)
        return bootstrap.Backends(self.deps, self.existing, {}, [], self.enum)

    def rank_client(self, key, years):
        self.rank_client_years.append(years)
        return self.fake_lastfm

    def enum(self, artist):
        self.calls.append(artist["name"])
        v = self.records[artist["name"]]
        if isinstance(v, Exception):
            raise v
        return v

    # --- helpers ---

    def make_ranking(self, names=NAMES, **kw):
        arts = [{"name": n, "mbid": None, "playcount": 1000 - i} for i, n in enumerate(names)]
        r = bootstrap_ranking.build(arts, kw.get("years", 5), kw.get("max_artists", 100),
                                    datetime.datetime(2026, 10, 11, 18, 0, 0, tzinfo=datetime.timezone.utc))
        bootstrap_ranking.save(None, r)
        return r

    def tracks(self, n_each=2, names=NAMES):
        self.records = {n: [lrec(n, f"{n} t{i}") for i in range(n_each)] for n in names}

    def config(self, body=None, name="bootstrap.yml") -> Path:
        path = self.root / name
        path.write_text(body if body is not None else f"lastfm_artists:\n  user: {USER}\n")
        return path

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return bootstrap.main(list(argv))
        finally:
            self.out, self.err = out.getvalue(), err.getvalue()

    def run_session(self, *extra, max_new="100", max_minutes="60"):
        return self.main("run", "--max-new", max_new, "--max-minutes", max_minutes, *extra)

    def progress(self) -> dict:
        return json.loads((self.dir / bootstrap.PROGRESS_FILE).read_text())

    def entry(self, name) -> dict:
        return self.progress()["artists"][ingest.lastfm_artist_key(name)]

    def snapshot(self) -> dict:
        """Every state file with its content; ingest.lock is the lock, not state (an empty file)."""
        return {str(p.relative_to(self.dir)): p.read_bytes() for p in self.dir.rglob("*")
                if p.is_file() and p.name != bootstrap.LOCK_FILE} if self.dir.exists() else {}

    def status(self, *extra) -> list:
        self.assertEqual(self.main("status", *extra), 0)
        return self.out.splitlines()


class UsageTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()

    def refused(self, *argv):
        self.assertEqual(self.main(*argv), 2, self.err)
        self.assertEqual(self.backend_calls, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.dir / bootstrap.PROGRESS_FILE).exists())

    def test_run_and_plan_require_both_caps(self):
        for sub in ("run", "plan"):
            self.refused(sub)
            self.refused(sub, "--max-new", "5")
            self.refused(sub, "--max-minutes", "5")

    def test_caps_must_be_in_range_and_whole_numbers(self):
        for sub in ("run", "plan"):
            for new, minutes in (("0", "5"), ("1000001", "5"), ("5", "0"), ("5", "1441"), ("-1", "5"), ("2.5", "5"),
                                 ("x", "5"), ("5", "nan")):
                self.refused(sub, "--max-new", new, "--max-minutes", minutes)

    def test_bounds_are_accepted(self):
        self.assertEqual(self.main("plan", "--max-new", "1000000", "--max-minutes", "1"), 0, self.err)

    def test_rank_and_status_take_no_caps_and_no_result_json(self):
        for sub in ("rank", "status"):
            self.assertEqual(self.main(sub, "--max-new", "5"), 2)
            self.assertEqual(self.main(sub, "--max-minutes", "5"), 2)
            self.assertEqual(self.main(sub, "--result-json", str(self.root / "r.json")), 2)
        self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5", "--result-json", "x"), 2)

    def test_abbreviations_are_not_accepted(self):
        self.assertEqual(self.main("run", "--max-n", "5", "--max-minutes", "5"), 2)
        self.assertEqual(self.main("run", "--max-new", "5", "--max-minutes", "5", "--result", "x"), 2)

    def test_unknown_subcommand_and_no_subcommand(self):
        self.assertEqual(self.main("frobnicate"), 2)
        self.assertEqual(self.main(), 2)

    def test_result_json_is_a_single_value_and_the_last_one_wins(self):
        a, b = self.root / "a.json", self.root / "b.json"
        self.assertEqual(self.run_session("--result-json", str(a), "--result-json", str(b)), 0, self.err)
        self.assertFalse(a.exists())
        self.assertIn("stop_reason", json.loads(b.read_text()))

    def test_no_ranking_exits_2_with_run_rank_first_and_never_computes_one(self):
        (self.dir / bootstrap_ranking.FILE_NAME).unlink()
        self.rank_client_years.clear()
        self.assertEqual(self.run_session(), 2)
        self.assertIn("run rank first", self.err)
        self.assertEqual(len(self.err.strip().splitlines()), 1)
        self.assertEqual((self.backend_calls, self.fake_lastfm.calls, self.rank_client_years), (0, 0, []))
        self.assertFalse((self.dir / bootstrap_ranking.FILE_NAME).exists())
        self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5"), 2)

    def test_corrupt_ranking_is_no_ranking_not_a_crash(self):
        (self.dir / bootstrap_ranking.FILE_NAME).write_text("{nope")
        self.assertEqual(self.run_session(), 2)
        self.assertIn("run rank first", self.err)


class EnvTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()

    def test_run_needs_the_ingest_environment_and_names_the_variable_not_a_value(self):
        del os.environ["TG_BOT_TOKEN"]
        self.assertEqual(self.run_session(), 2)
        self.assertIn("TG_BOT_TOKEN", self.err)
        self.assertEqual(self.backend_calls, 0)

    def test_plan_needs_no_telegram(self):
        del os.environ["TG_BOT_TOKEN"], os.environ["TG_CHANNEL_ID"]
        self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5"), 0, self.err)

    def test_missing_or_malformed_lastfm_key(self):
        del os.environ["LASTFM_API_KEY"]
        self.assertEqual(self.run_session(), 2)
        self.assertIn("LASTFM_API_KEY", self.err)
        os.environ["LASTFM_API_KEY"] = "short key!"
        self.assertEqual(self.run_session(), 2)
        self.assertNotIn("short key", self.err)

    def test_env_syntax_error_is_exit_2(self):
        def bad():
            raise tg_check.EnvSyntaxError(".env line 3: unsupported syntax")
        with mock.patch.object(tg_check, "load_env", bad):
            self.assertEqual(self.run_session(), 2)
        self.assertIn("line 3", self.err)


class RankTest(Base):
    def test_stores_the_ranking_privately_with_no_user_name(self):
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  years: 3\n  max_artists: 2\n")
        self.assertEqual(self.main("rank", "--config", str(cfg)), 0, self.err)
        path = self.dir / bootstrap_ranking.FILE_NAME
        got = json.loads(path.read_text())
        self.assertEqual((got["years"], got["max_artists"], [a["name"] for a in got["artists"]]),
                         (3, 2, ["Band 1", "Band 2"]))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.rank_client_years, [3])
        self.assertNotIn(USER, path.read_text() + self.out + self.err)
        self.assertNotIn(KEY, self.out + self.err)

    def test_second_rank_is_refused_without_refresh_and_makes_no_call(self):
        cfg = self.config()
        self.assertEqual(self.main("rank", "--config", str(cfg)), 0)
        before, calls = (self.dir / bootstrap_ranking.FILE_NAME).read_bytes(), self.fake_lastfm.calls
        self.assertEqual(self.main("rank", "--config", str(cfg)), 2)
        self.assertIn("--refresh", self.err)
        self.assertEqual(self.fake_lastfm.calls, calls)
        self.assertEqual((self.dir / bootstrap_ranking.FILE_NAME).read_bytes(), before)

    def test_refresh_replaces_and_a_failed_refresh_keeps_the_old_file(self):
        cfg = self.config()
        self.assertEqual(self.main("rank", "--config", str(cfg)), 0)
        self.fake_lastfm = FakeLastfm(artists=(("Other", 5),))
        self.assertEqual(self.main("rank", "--config", str(cfg), "--refresh"), 0, self.err)
        self.assertEqual([a["name"] for a in bootstrap_ranking.load()["artists"]], ["Other"])
        self.fake_lastfm = FakeLastfm(fail=True)
        before = (self.dir / bootstrap_ranking.FILE_NAME).read_bytes()
        self.assertEqual(self.main("rank", "--config", str(cfg), "--refresh"), 1)
        self.assertEqual((self.dir / bootstrap_ranking.FILE_NAME).read_bytes(), before)
        self.assertNotIn(USER, self.out + self.err)

    def test_rank_needs_a_config_with_a_user(self):
        self.assertEqual(self.main("rank"), 2)
        self.assertIn("--config", self.err)
        self.assertEqual(self.main("rank", "--config", str(self.root / "missing.yml")), 2)
        self.assertEqual(self.main("rank", "--config", str(self.config("lastfm_artists:\n  years: 3\n"))), 2)
        self.assertEqual(self.fake_lastfm.calls, 0)

    def test_client_budget_is_sized_for_the_ranking_never_the_default_400(self):
        for years in (1, 5, 7, 10):
            client = REAL_RANK_CLIENT(KEY, years)
            self.assertGreaterEqual(client._max_calls, 1 + years * lastfm.WEEKS_PER_YEAR_BOUND + 4)
        self.assertGreater(REAL_RANK_CLIENT(KEY, 10)._max_calls, lastfm.MAX_CALLS)


REAL_RANK_CLIENT = bootstrap.rank_client  # the real function, captured before any test patches the module attribute


class SessionTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()

    def test_a_clean_session_ends_done_and_writes_progress_state_and_result(self):
        result = self.root / "result.json"
        self.assertEqual(self.run_session("--result-json", str(result)), 0, self.err)
        self.assertEqual(self.calls, NAMES)
        self.assertEqual(len(self.f.uploaded), 6)
        prog = self.progress()
        self.assertEqual((prog["version"], prog["sessions"], prog["ranking_computed_at"]), (1, 1, "2026-10-11T18:00:00Z"))
        for n in NAMES:
            self.assertEqual(self.entry(n), {"status": "done", "enum": "ok", "truncated": False, "tracks_added": 2,
                                             "tracks_seen": 2, "failures": 0, "updated": "2026-10-11"})
        self.assertEqual(self.status(), ["done: 3 artists, 6 tracks"])
        got = json.loads(result.read_text())
        self.assertEqual(got["stop_reason"], "done")
        self.assertEqual((got["exit_code"], got["counts"]["added"], got["counts"]["failed"]), (0, 6, 0))
        self.assertEqual(set(got["counts"]), {"added", "skipped", "no_match", "too_big", "failed", "matched_title_only",
                                              "cooldown", "not_attempted"})
        self.assertEqual(got["counts"]["matched_title_only"], 6)
        self.assertEqual(self.planned_sizes, [3])  # the client is sized for the planned artists

    def test_files_are_private_and_the_engine_state_is_separate(self):
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        for name in (bootstrap.PROGRESS_FILE, bootstrap.STATE_FILE):
            p = self.dir / name
            if p.exists():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, name)

    def test_the_nightly_state_file_is_never_read_or_written(self):
        nightly = self.dir / "ingest-state.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        nightly.write_text("{not json at all")
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(nightly.read_text(), "{not json at all")
        nightly.unlink()
        self.assertEqual(self.run_session(), 0)
        self.assertFalse(nightly.exists())  # a run does not create it either

    def test_cap_stop_then_resume_adds_only_what_is_missing(self):
        result = self.root / "r.json"
        self.assertEqual(self.run_session("--result-json", str(result), max_new="3"), 0, self.err)
        self.assertEqual(json.loads(result.read_text())["stop_reason"], "cap")
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 2")["status"]), ("done", "partial"))
        self.assertEqual(self.entry("Band 2")["tracks_added"], 1)
        self.assertNotIn(ingest.lastfm_artist_key("Band 3"), self.progress()["artists"])
        self.assertEqual(self.status()[0], "partial: 1 of 3 artists done, 3 tracks, next: 2")
        self.calls.clear()
        self.assertEqual(self.run_session("--result-json", str(result), max_new="100"), 0)
        self.assertEqual(self.calls, ["Band 2", "Band 3"])  # the partial artist first; the done one is not handed in
        self.assertEqual(json.loads(result.read_text())["stop_reason"], "done")
        self.assertEqual(len(self.f.uploaded), 6)
        self.assertEqual(len(set(self.f.uploaded)), 6)  # each track exactly once
        self.assertEqual(self.entry("Band 2")["tracks_added"], 2)
        self.assertEqual(self.progress()["sessions"], 2)
        self.assertEqual(self.status(), ["done: 3 artists, 6 tracks"])

    def test_time_stop_leaves_the_artist_partial_and_exit_0(self):
        clock = [0.0]
        inner = self.deps.download

        def slow(song, m, tdir):
            clock[0] += 100
            return inner(song, m, tdir)
        self.deps.download = slow
        with mock.patch.object(bootstrap, "make_limits", lambda n, m: ingest.RunLimits(n, m, lambda: clock[0])):
            result = self.root / "r.json"
            self.assertEqual(self.run_session("--result-json", str(result), max_minutes="1"), 0, self.err)
        self.assertEqual(json.loads(result.read_text())["stop_reason"], "time")
        self.assertEqual(self.entry("Band 1")["status"], "partial")
        self.assertEqual(self.entry("Band 1")["tracks_added"], 1)
        self.assertEqual(self.calls, ["Band 1"])
        self.deps.download = inner
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, NAMES)  # resumes with the partial artist first
        self.assertEqual(len(set(self.f.uploaded)), 6)

    def test_engine_scan_date_does_not_decide_what_is_worked(self):
        # the engine's own state says Band 1 was scanned today (a crash between its write and the hook's), the
        # progress file does not say done: the artist must be worked (rescan_days=0), not skipped as "scanned recently"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / bootstrap.STATE_FILE).write_text(json.dumps(
            {"version": 1, "tracks": {}, "artists": {ingest.lastfm_artist_key("Band 1"): ingest.utc_today().isoformat()}}))
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(self.calls[0], "Band 1")
        self.assertEqual(self.entry("Band 1")["status"], "done")

    def test_rescan_days_in_the_config_has_no_meaning(self):
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  rescan_days: 3650\n")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / bootstrap.STATE_FILE).write_text(json.dumps(
            {"version": 1, "tracks": {}, "artists": {ingest.lastfm_artist_key("Band 1"): ingest.utc_today().isoformat()}}))
        self.assertEqual(self.run_session("--config", str(cfg)), 0, self.err)
        self.assertEqual(self.calls[0], "Band 1")

    def test_nothing_to_do_when_everything_is_done(self):
        self.assertEqual(self.run_session(), 0)
        self.calls.clear()
        self.backend_calls = 0
        result = self.root / "r.json"
        self.assertEqual(self.run_session("--result-json", str(result)), 0)
        self.assertEqual((self.calls, self.backend_calls), ([], 0))
        self.assertEqual(json.loads(result.read_text())["stop_reason"], "done")
        self.assertEqual(self.progress()["sessions"], 1)

    def test_a_failed_track_counts_exit_1_and_the_artist_still_ends_done(self):
        self.f.fail_upload = {lrec("Band 1", "Band 1 t0")["song_id"]}
        self.assertEqual(self.run_session(), 1)
        self.assertEqual(self.entry("Band 1")["status"], "done")
        self.assertEqual(self.entry("Band 1")["tracks_added"], 1)

    def test_the_ranking_refresh_keeps_statuses_and_drops_leavers_from_the_count(self):
        self.assertEqual(self.run_session(), 0)
        self.make_ranking(["Band 2", "Band 9"])
        self.tracks(names=["Band 9"])
        self.assertEqual(self.status()[0], "partial: 1 of 2 artists done, 6 tracks, next: 2")  # M = all tracks added
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, ["Band 9"])
        self.assertIn(ingest.lastfm_artist_key("Band 1"), self.progress()["artists"])  # a leaver stays in the file
        self.assertEqual(self.status(), ["done: 2 artists, 8 tracks"])

    def test_unusable_artist_name_is_recorded_not_enumerated(self):
        self.make_ranking(["Band 1", "!!!"])
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(self.calls, ["Band 1"])
        e = self.entry("!!!")
        self.assertEqual((e["status"], e["enum"], e["tracks_added"]), ("done", "unusable_artist", 0))
        self.assertEqual(self.status()[1], "empty: 1 artists with no tracks found (unknown or no albums), truncated: 0 artists")


class EnumerationStatusTest(Base):
    def test_unknown_no_albums_and_truncated_are_recorded_and_never_a_clean_done(self):
        self.make_ranking(["Band 1", "Band 2", "Band 3", "Band 4"])
        self.records = {"Band 1": at([], "unknown_artist"), "Band 2": at([], "no_albums"),
                        "Band 3": at([lrec("Band 3", "x")], "ok", True), "Band 4": at([lrec("Band 4", "y")])}
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(self.entry("Band 1")["enum"], "unknown_artist")
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 1")["tracks_added"]), ("done", 0))
        self.assertEqual(self.entry("Band 2")["enum"], "no_albums")
        self.assertEqual((self.entry("Band 3")["enum"], self.entry("Band 3")["truncated"]), ("ok", True))
        lines = self.status()
        self.assertEqual(lines, ["done: 4 artists, 2 tracks",
                                 "empty: 2 artists with no tracks found (unknown or no albums), truncated: 1 artists"])

    def test_a_truncated_only_library_still_gets_the_second_line(self):
        self.make_ranking(["Band 1"])
        self.records = {"Band 1": at([lrec("Band 1", "x")], "ok", True)}
        self.run_session()
        self.assertEqual(self.status()[1], "empty: 0 artists with no tracks found (unknown or no albums), truncated: 1 artists")


class HookTest(Base):
    """bootstrap.Recorder used directly through the real engine."""

    def engine(self, artists, recorder, records=None):
        self.records = records or {}
        state = ingest.RunState(None)
        lr = ingest.LastfmRun()
        with contextlib.redirect_stdout(io.StringIO()):
            ingest.run_lastfm(lr, ingest.LastfmConfig(user="u1"), artists, self.enum, [], self.deps, set(), {},
                              self.root / "work", False, None, state, on_artist=recorder.on_artist, rescan_days=0)
        return lr

    def test_names_that_share_one_key_do_not_collide(self):
        prog = bootstrap.Progress(self.dir / bootstrap.PROGRESS_FILE)
        rec = bootstrap.Recorder(prog)
        key = ingest.lastfm_artist_key("!!!")
        self.assertEqual(key, ingest.lastfm_artist_key("???"))
        self.engine([rank(1, "!!!"), rank(2, "???"), rank(3)], rec, {"Band 3": [lrec("Band 3", "c")]})
        self.assertEqual(rec.errors, 0)
        self.assertEqual(set(prog.artists), {key, ingest.lastfm_artist_key("Band 3")})
        self.assertEqual((prog.artists[key]["status"], prog.artists[key]["enum"], prog.artists[key]["tracks_added"]),
                         ("done", "unusable_artist", 0))
        self.assertEqual(prog.artists[ingest.lastfm_artist_key("Band 3")]["status"], "done")

    def test_the_hook_never_raises_whatever_goes_wrong(self):
        prog = bootstrap.Progress(self.dir / bootstrap.PROGRESS_FILE)
        rec = bootstrap.Recorder(prog)
        with mock.patch.object(bootstrap.Progress, "save", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stdout(io.StringIO()):
            rec.on_artist(rank(1), ingest.ArtistReport(ingest.lastfm_artist_key("Band 1"), "done", "ok", False, {}))
        self.assertEqual(rec.errors, 1)  # logged and counted, never raised
        self.assertEqual(prog.status(ingest.lastfm_artist_key("Band 1")), "done")  # kept in memory

    def test_a_malformed_report_is_counted_not_raised(self):
        rec = bootstrap.Recorder(bootstrap.Progress(self.dir / bootstrap.PROGRESS_FILE))
        with contextlib.redirect_stdout(io.StringIO()):
            rec.on_artist(rank(1), None)
        self.assertEqual(rec.errors, 1)

    def test_the_sign_in_branch_keeps_exit_3_even_when_the_hook_fails(self):
        self.make_ranking(["Band 1", "Band 2"])
        self.tracks(names=["Band 1", "Band 2"])
        self.deps.download = self.sign_in
        real, n = bootstrap.Progress.save, [0]

        def flaky(p):
            n[0] += 1
            if n[0] > 1:  # the session-start write works, every hook write fails
                raise RuntimeError("disk exploded")
            real(p)
        with mock.patch.object(bootstrap.Progress, "save", flaky):
            self.assertEqual(self.run_session(), 3, self.err)
        self.assertIn("youtube-sign-in", self.out)

    def sign_in(self, song, m, tdir):
        self.downloads = getattr(self, "downloads", 0) + 1
        raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")

    def test_a_failed_write_is_logged_exit_1_and_the_session_goes_on(self):
        self.make_ranking(["Band 1", "Band 2"])
        self.tracks(names=["Band 1", "Band 2"])
        real, n = bootstrap.Progress.save, [0]

        def once(p):
            n[0] += 1
            if n[0] == 2:  # the first hook write
                raise OSError("disk full")
            real(p)
        with mock.patch.object(bootstrap.Progress, "save", once):
            code = self.run_session()
        self.assertEqual(code, 0)  # the later write succeeded and carries everything: nothing is lost
        self.assertIn("could not write", self.out)
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 2")["status"]), ("done", "done"))

    def test_an_unsaved_last_write_is_exit_1(self):
        self.make_ranking(["Band 1"])
        self.tracks(names=["Band 1"])
        real, n = bootstrap.Progress.save, [0]

        def last_fails(p):
            n[0] += 1
            if n[0] == 2:
                raise OSError("disk full")
            real(p)
        with mock.patch.object(bootstrap.Progress, "save", last_fails):
            self.assertEqual(self.run_session(), 1)
        self.assertIn("not fully saved", self.out)


class SignInTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()
        self.downloads = 0
        self.inner = self.deps.download

        def challenged(song, m, tdir):
            self.downloads += 1
            raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
        self.deps.download = challenged

    def test_sign_in_stops_the_session_with_exit_3_once_and_never_retries(self):
        result = self.root / "r.json"
        self.assertEqual(self.run_session("--result-json", str(result)), 3, self.err)
        self.assertEqual(self.downloads, 1)  # no retry, no second track, no second artist
        self.assertEqual(self.calls, ["Band 1"])
        self.assertEqual(json.loads(result.read_text())["stop_reason"], "sign-in")
        self.assertEqual(json.loads(result.read_text())["exit_code"], 3)
        self.assertEqual(self.entry("Band 1")["status"], "partial")
        self.assertIn("do not add cookies", self.out)
        self.assertEqual(self.status()[0], "partial: 0 of 3 artists done, 0 tracks, next: 1")

    def test_the_next_session_resumes_after_a_sign_in_stop(self):
        self.run_session()
        self.deps.download = self.inner
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, NAMES)
        self.assertEqual(self.status(), ["done: 3 artists, 6 tracks"])

    def test_the_wrapper_reads_stop_reason_at_the_top_level(self):
        result = self.root / "r.json"
        self.run_session("--result-json", str(result))
        self.assertIsInstance(json.loads(result.read_text())["stop_reason"], str)


class FailureTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking(["Band 1", "Band 2"])
        self.records = {"Band 1": lastfm.LastfmError("refused"), "Band 2": [lrec("Band 2", "c")]}

    def test_three_failures_park_the_artist_and_it_is_skipped_next_session(self):
        for n in (1, 2):
            self.assertEqual(self.run_session(), 1)
            self.assertEqual((self.entry("Band 1")["failures"], self.entry("Band 1")["status"]), (n, "pending"))
        self.assertEqual(self.run_session(), 1)
        self.assertEqual((self.entry("Band 1")["failures"], self.entry("Band 1")["status"]), (3, "parked"))
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)  # nothing failed this time: Band 1 is not tried
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status(), ["done: 2 artists, 1 tracks, 1 parked"])

    def test_status_while_work_is_left_names_the_parked_count(self):
        for _ in range(3):
            self.run_session()  # Band 1 fails three times and is parked, Band 2 is done
        self.make_ranking(["Band 1", "Band 2", "Band 4"])
        self.assertEqual(self.status(), ["partial: 1 of 3 artists done, 1 tracks, next: 3, 1 parked"])

    def test_unpark_makes_the_artist_pending_and_a_success_resets_failures(self):
        for _ in range(3):
            self.run_session()
        self.records["Band 1"] = [lrec("Band 1", "a")]
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls.count("Band 1"), 3)  # still parked: no fourth attempt
        self.calls.clear()
        self.assertEqual(self.run_session("--unpark"), 0, self.err)
        self.assertEqual(self.calls, ["Band 1"])
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 1")["failures"]), ("done", 0))

    def test_unpark_resets_failures_so_one_more_failure_does_not_park_again(self):
        for _ in range(3):
            self.run_session()
        self.assertEqual(self.entry("Band 1")["status"], "parked")
        self.assertEqual(self.run_session("--unpark"), 1)  # Band 1 fails once more
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 1")["failures"]), ("pending", 1))
        self.calls.clear()
        self.run_session()
        self.assertEqual(self.calls, ["Band 1"])  # still worked: failures 2, not parked

    def test_a_success_resets_the_failure_count(self):
        self.run_session()
        self.run_session()
        self.assertEqual(self.entry("Band 1")["failures"], 2)
        self.records["Band 1"] = [lrec("Band 1", "a")]
        self.assertEqual(self.run_session(), 0)
        self.assertEqual((self.entry("Band 1")["status"], self.entry("Band 1")["failures"]), ("done", 0))

    def test_failures_must_be_consecutive(self):
        self.run_session()
        self.run_session()
        self.records["Band 1"] = [lrec("Band 1", "a")]
        self.run_session()
        self.records["Band 1"] = lastfm.LastfmError("refused")
        self.make_ranking(["Band 1", "Band 2", "Band 3"])
        self.assertEqual(self.entry("Band 1")["failures"], 0)

    def test_excluding_a_parked_artist_also_removes_it_from_the_count(self):
        for _ in range(3):
            self.run_session()
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  exclude:\n    - band 1\n")
        self.assertEqual(self.status("--config", str(cfg)), ["done: 1 artists, 1 tracks"])


class StateFileTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()
        self.dir.mkdir(parents=True, exist_ok=True)

    GOOD_PROGRESS = {"version": 1, "ranking_computed_at": "2026-10-11T18:00:00Z", "sessions": 1, "artists": {}}
    ENTRY = {"status": "done", "enum": "ok", "truncated": False, "tracks_added": 1, "tracks_seen": 1, "failures": 0,
             "updated": "2026-10-11"}
    GOOD_STATE = {"version": 1, "tracks": {"lfm:0123456789abcdef": {"status": "no_match", "retry_after": "2099-01-01"}},
                  "artists": {"lfm:0123456789abcdef": "2026-10-10"}}

    def bad_variants(self, which):
        good = dict(self.GOOD_PROGRESS if which == "progress" else self.GOOD_STATE)
        key = "lfm:fedcba9876543210"
        variants = {"not json": "{nope", "a list": "[]", "version 2": {**good, "version": 2},
                    "no version": {k: v for k, v in good.items() if k != "version"}, "extra field": {**good, "extra": 1},
                    "nan": '{"version": NaN}'}
        if which == "progress":
            variants["bad entry"] = {**good, "artists": {key: {**self.ENTRY, "status": "finished"}}}
            variants["extra entry key"] = {**good, "artists": {key: {**self.ENTRY, "name": "x"}}}
            variants["bad key"] = {**good, "artists": {"Band 1": self.ENTRY}}
            variants["negative count"] = {**good, "artists": {key: {**self.ENTRY, "tracks_added": -1}}}
            variants["bool count"] = {**good, "artists": {key: {**self.ENTRY, "tracks_added": True}}}
            variants["bad date"] = {**good, "artists": {key: {**self.ENTRY, "updated": "yesterday"}}}
        else:
            variants["bad track"] = {**good, "tracks": {**good["tracks"], "lfm:0123456789abcde": {"status": "failed", "retry_after": "2099-01-01"}}}
            variants["bad status"] = {**good, "tracks": {"lfm:0123456789abcdef": {"status": "new", "retry_after": "2099-01-01"}}}
            variants["bad retry date"] = {**good, "tracks": {"lfm:0123456789abcdef": {"status": "failed", "retry_after": "soon"}}}
            variants["bad artist"] = {**good, "artists": {"nope": "2026-10-10"}}
            variants["tracks not object"] = {**good, "tracks": []}
        return variants

    def write(self, which, content):
        path = self.dir / (bootstrap.PROGRESS_FILE if which == "progress" else bootstrap.STATE_FILE)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    def test_run_refuses_every_unusable_file_with_one_line_and_touches_nothing(self):
        for which in ("progress", "state"):
            for label, content in self.bad_variants(which).items():
                with self.subTest(which=which, case=label):
                    path = self.write(which, content)
                    before = self.snapshot()
                    self.assertEqual(self.run_session(), 2, self.err)
                    self.assertEqual(len(self.err.strip().splitlines()), 1)
                    self.assertIn("state file unusable", self.err)
                    self.assertEqual(self.backend_calls, 0)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5"), 2)
                    path.unlink()

    def test_status_says_state_file_unusable_exit_0(self):
        for which in ("progress", "state"):
            for label, content in self.bad_variants(which).items():
                with self.subTest(which=which, case=label):
                    path = self.write(which, content)
                    lines = self.status()
                    self.assertEqual(len(lines), 1)
                    self.assertTrue(lines[0].startswith("state file unusable: "), lines)
                    path.unlink()

    def test_stricter_than_the_engine_which_would_silently_drop_the_entry(self):
        bad = {**self.GOOD_STATE, "tracks": {**self.GOOD_STATE["tracks"], "short": {"status": "failed", "retry_after": "2099-01-01"}}}
        path = self.write("state", bad)
        with contextlib.redirect_stdout(io.StringIO()):
            st = ingest.RunState.load(path)
        self.assertEqual(len(st.entries), 1)  # RunState.load starts and drops what it does not understand
        self.assertEqual(self.run_session(), 2)  # bootstrap refuses

    def test_too_big_and_not_a_regular_file(self):
        self.write("progress", self.GOOD_PROGRESS)
        with mock.patch.object(bootstrap, "PROGRESS_MAX_BYTES", 10):
            self.assertEqual(self.run_session(), 2)
            self.assertIn("larger than", self.err)
            self.assertTrue(self.status()[0].startswith("state file unusable"))
        (self.dir / bootstrap.PROGRESS_FILE).unlink()
        self.write("state", self.GOOD_STATE)
        with mock.patch.object(ingest, "STATE_MAX_BYTES", 10):
            self.assertEqual(self.run_session(), 2)
        (self.dir / bootstrap.STATE_FILE).unlink()
        (self.dir / bootstrap.PROGRESS_FILE).mkdir()
        self.assertEqual(self.run_session(), 2)
        self.assertIn("not a regular file", self.err)

    def test_valid_files_are_accepted(self):
        self.write("progress", {**self.GOOD_PROGRESS, "artists": {ingest.lastfm_artist_key("Band 1"): self.ENTRY}})
        self.write("state", self.GOOD_STATE)
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(self.calls, ["Band 2", "Band 3"])  # Band 1 is done in the progress file
        self.assertEqual(self.progress()["sessions"], 2)

    def test_reset_state_moves_both_files_aside_with_numbered_backups_never_overwriting(self):
        wanted = []
        for n in range(3):
            content = {**self.GOOD_PROGRESS, "version": 7 + n}  # unusable, and distinct each time
            self.write("progress", content)
            self.write("state", {"junk": n})
            self.assertEqual(self.run_session("--reset-state"), 0, self.err)
            wanted.append(content)
            self.calls.clear()
        names = ["bootstrap-progress.json.bak", "bootstrap-progress.json.bak.1", "bootstrap-progress.json.bak.2"]
        for name, content in zip(names, wanted):
            self.assertEqual(json.loads((self.dir / name).read_text()), content)
        self.assertEqual(json.loads((self.dir / "bootstrap-state.json.bak.2").read_text()), {"junk": 2})
        self.assertEqual(self.progress()["version"], 1)  # a fresh, valid file after the reset
        self.assertFalse((self.dir / "bootstrap-progress.json.bak.3").exists())

    def test_reset_state_starts_empty_and_leaves_ranking_and_nightly_state_alone(self):
        self.assertEqual(self.run_session(), 0)
        (self.dir / "ingest-state.json").write_text("nightly")
        ranking = (self.dir / bootstrap_ranking.FILE_NAME).read_bytes()
        self.calls.clear()
        self.assertEqual(self.run_session("--reset-state"), 0)
        self.assertEqual(self.calls, NAMES)  # started empty: everything is worked again (D1 skips the stored tracks)
        self.assertEqual(self.progress()["sessions"], 1)
        self.assertEqual((self.dir / "ingest-state.json").read_text(), "nightly")
        self.assertEqual((self.dir / bootstrap_ranking.FILE_NAME).read_bytes(), ranking)
        self.assertEqual(len(self.f.uploaded), 6)  # no track was uploaded twice

    def test_reset_state_without_the_ranking_touches_nothing(self):
        (self.dir / bootstrap_ranking.FILE_NAME).unlink()
        self.write("progress", "{bad")
        self.assertEqual(self.run_session("--reset-state"), 2)
        self.assertEqual((self.dir / bootstrap.PROGRESS_FILE).read_text(), "{bad")


class StatusTest(Base):
    def test_no_ranking_and_unusable_ranking_are_one_line_exit_0(self):
        lines = self.status()
        self.assertEqual(len(lines), 1)
        self.assertIn("no usable ranking", lines[0])
        self.assertFalse(self.dir.exists() and any(self.dir.iterdir()))  # nothing written
        self.make_ranking()
        (self.dir / bootstrap_ranking.FILE_NAME).write_text("{")
        self.assertEqual(len(self.status()), 1)

    def test_a_fresh_ranking_is_partial_with_next_1(self):
        self.make_ranking()
        self.assertEqual(self.status(), ["partial: 0 of 3 artists done, 0 tracks, next: 1"])

    def test_exclude_leaves_artists_out_of_the_count_by_normalised_name(self):
        self.make_ranking()
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  exclude:\n    - BAND  1\n")
        self.assertEqual(self.status("--config", str(cfg)), ["partial: 0 of 2 artists done, 0 tracks, next: 2"])

    def test_a_bad_config_is_reported_on_one_line(self):
        self.make_ranking()
        lines = self.status("--config", str(self.root / "nope.yml"))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("config unusable"))

    def test_status_does_no_network_and_loads_no_env(self):
        self.make_ranking()
        with mock.patch.object(tg_check, "load_env", side_effect=AssertionError(".env read")):
            self.assertEqual(self.main("status"), 0)
        self.assertEqual((self.backend_calls, self.fake_lastfm.calls), (0, 0))

    def test_output_holds_no_names_or_titles(self):
        self.make_ranking()
        self.tracks()
        self.run_session("--result-json", str(self.root / "r.json"))
        self.status()
        seen = self.out + (self.dir / bootstrap.PROGRESS_FILE).read_text() + (self.root / "r.json").read_text()
        for needle in ("Band 1", "Band 2", "t0", USER, KEY):
            self.assertNotIn(needle, seen)


class RealBackendsTest(Base):
    """The real network side with its pieces faked: the Last.fm client is sized for the session, full results."""

    def test_the_client_budget_is_150_per_planned_artist_and_the_enumerator_returns_the_status(self):
        import ytmusicapi
        sized, wrapped = [], ingest.lastfm_session_client

        def spy(key, planned, ranking_calls=0, **kw):
            sized.append(planned)
            return wrapped(key, planned, ranking_calls, **kw)
        enumerators = []
        with mock.patch.object(ingest, "lastfm_session_client", spy), \
                mock.patch.object(ingest, "d1_query", lambda sql, params=None: []), \
                mock.patch.object(ingest, "real_deps", lambda ytm, throttle: self.deps), \
                mock.patch.object(ingest, "real_lastfm_enumerator",
                                  lambda client, full=False: enumerators.append((client._max_calls, full))), \
                mock.patch.object(ytmusicapi, "YTMusic", lambda: object()):
            got = REAL_BACKENDS(7)
        self.assertEqual(sized, [7])
        self.assertEqual(enumerators, [(7 * ingest.LASTFM_ARTIST_CALLS, True)])
        self.assertEqual((got.existing, got.stored_rows), (set(), []))


REAL_BACKENDS = bootstrap.real_backends


class PlanTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks(3)

    def test_plan_writes_nothing_downloads_nothing_and_takes_no_lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / bootstrap.STATE_FILE).write_text(json.dumps({"version": 1, "tracks": {}}))
        before = self.snapshot()
        held = bootstrap.take_lock(self.dir)  # a plan runs while another process holds the lock
        self.addCleanup(os.close, held)
        before = self.snapshot()
        self.assertEqual(self.main("plan", "--max-new", "4", "--max-minutes", "60"), 0, self.err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.f.uploaded, self.f.deleted, self.f.batches), ([], [], []))
        self.assertEqual(self.existing, set())
        self.assertIn("would attempt 4 track(s)", self.out)
        self.assertIn("nothing was downloaded, uploaded or written", self.out)

    def test_plan_counts_matches_against_max_new_and_reports_the_artists_reached(self):
        self.assertEqual(self.main("plan", "--max-new", "4", "--max-minutes", "60"), 0)
        self.assertIn("would reach 2 of 3 artists (rank positions 1-2)", self.out)
        self.assertIn("1 complete, 1 cut by a limit", self.out)
        self.assertEqual(self.calls, ["Band 1", "Band 2"])  # the third artist is not even enumerated

    def test_plan_skips_done_parked_and_excluded_artists(self):
        self.assertEqual(self.run_session(max_new="3"), 0)  # Band 1 done
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  exclude:\n    - Band 3\n")
        self.calls.clear()
        self.assertEqual(self.main("plan", "--max-new", "100", "--max-minutes", "60", "--config", str(cfg)), 0)
        self.assertEqual(self.calls, ["Band 2"])

    def test_plan_with_nothing_left(self):
        self.run_session()
        self.calls.clear()
        self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5"), 0)
        self.assertIn("nothing to do", self.out)
        self.assertEqual(self.calls, [])

    def test_plan_does_not_mark_anything_so_the_session_after_it_is_unchanged(self):
        self.main("plan", "--max-new", "100", "--max-minutes", "60")
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, NAMES)
        self.assertEqual(len(self.f.uploaded), 9)

    def test_plan_stops_on_a_sign_in_challenge_with_exit_3(self):
        def challenged(song):
            raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
        self.deps.find_match = challenged
        self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "5"), 3)

    def test_plan_reports_a_failed_enumeration(self):
        self.records["Band 1"] = lastfm.LastfmError("refused")
        self.assertEqual(self.main("plan", "--max-new", "100", "--max-minutes", "60"), 1)
        self.assertEqual(self.entry_missing(), True)

    def entry_missing(self):
        return not (self.dir / bootstrap.PROGRESS_FILE).exists()


class LockTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()
        self.held = None

    def hold(self):
        self.held = bootstrap.take_lock(self.dir)
        self.addCleanup(lambda: os.close(self.held) if self.held is not None else None)

    def release(self):
        os.close(self.held)
        self.held = None

    def test_a_direct_run_takes_the_lock_and_exits_75_when_it_is_held(self):
        self.hold()
        self.assertEqual(self.run_session(), 75)
        self.assertEqual(len(self.err.strip().splitlines()), 1)
        self.assertEqual(self.backend_calls, 0)
        self.assertFalse((self.dir / bootstrap.PROGRESS_FILE).exists())

    def test_a_direct_run_holds_the_lock_while_working_and_releases_it_after(self):
        seen = []
        inner = self.deps.download

        def spy(song, m, tdir):
            try:
                os.close(bootstrap.take_lock(self.dir))
                seen.append("free")
            except bootstrap.Locked:
                seen.append("held")
            return inner(song, m, tdir)
        self.deps.download = spy
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(set(seen), {"held"})
        os.close(bootstrap.take_lock(self.dir))  # released after the session

    def test_the_lock_is_released_after_a_crash(self):
        def die(song, m, tdir):
            raise Boom()
        self.deps.download = die
        with self.assertRaises(Boom):
            self.run_session()
        os.close(bootstrap.take_lock(self.dir))

    def test_via_the_wrapper_the_lock_is_not_taken_again(self):
        self.hold()  # the wrapper holds it (another open file description: a second flock would fail)
        os.environ["COWLIC_LOCK_HELD"] = "1"
        self.assertEqual(self.run_session(), 0, self.err)

    def test_only_the_value_1_means_held(self):
        self.hold()
        os.environ["COWLIC_LOCK_HELD"] = "0"
        self.assertEqual(self.run_session(), 75)
        os.environ["COWLIC_LOCK_HELD"] = ""
        self.assertEqual(self.run_session(), 75)

    def test_a_value_from_dot_env_is_ignored(self):
        def env_file():
            os.environ.setdefault("COWLIC_LOCK_HELD", "1")  # what load_env does with a line in .env
        self.hold()
        with mock.patch.object(tg_check, "load_env", env_file):
            self.assertEqual(self.run_session(), 75)
        self.assertEqual(self.backend_calls, 0)

    def test_rank_refresh_takes_the_lock_too(self):
        cfg = self.config()
        self.assertEqual(self.main("rank", "--config", str(cfg), "--refresh"), 0, self.err)
        self.hold()
        self.assertEqual(self.main("rank", "--config", str(cfg), "--refresh"), 75)
        os.environ["COWLIC_LOCK_HELD"] = "1"
        self.assertEqual(self.main("rank", "--config", str(cfg), "--refresh"), 0, self.err)

    def test_status_never_takes_the_lock(self):
        self.hold()
        self.assertEqual(self.main("status"), 0)


class GuardTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()

    def at(self, hour, minute, second=0):
        return mock.patch.object(bootstrap, "local_now",
                                 lambda: datetime.datetime(2026, 10, 11, hour, minute, second))

    def test_refuses_when_the_session_plus_margin_reaches_21_10(self):
        with self.at(20, 0):
            self.assertEqual(self.run_session(max_minutes="59"), 78)  # 20:00 + 59 + 10 + 1 = 21:10
            self.assertEqual(len(self.err.strip().splitlines()), 1)
            self.assertIn("at most 58 minutes fit", self.err)
            self.assertEqual(self.run_session(max_minutes="58"), 0, self.err)

    def test_nothing_is_touched_on_a_refusal(self):
        with self.at(21, 0):
            before = self.snapshot()
            result = self.root / "r.json"
            self.assertEqual(self.run_session("--result-json", str(result), max_minutes="1"), 78)
            self.assertFalse(result.exists())  # the wrapper then reports no result at all
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.backend_calls, 0)

    def test_the_margin_is_ten_minutes_plus_the_timers_random_delay(self):
        for now, minutes, code in (((20, 30), 28, 0), ((20, 30), 29, 78)):
            with self.subTest(now=now, minutes=minutes), self.at(*now):
                self.assertEqual(self.run_session(max_minutes=str(minutes)), code, self.err)

    def test_within_60_seconds_after_21_10_that_days_run_still_counts(self):
        with self.at(21, 10, 30):
            self.assertEqual(self.run_session(max_minutes="1"), 78)

    def test_after_that_the_next_day_counts(self):
        with self.at(21, 12):
            self.assertEqual(self.run_session(max_minutes="1000"), 0, self.err)
            self.assertEqual(self.run_session(max_minutes="1440"), 78)

    def test_the_guard_is_checked_after_the_lock(self):
        held = bootstrap.take_lock(self.dir)
        self.addCleanup(os.close, held)
        with self.at(21, 0):
            self.assertEqual(self.run_session(max_minutes="1"), 75)

    def test_plan_is_not_guarded(self):
        with self.at(21, 0):
            self.assertEqual(self.main("plan", "--max-new", "5", "--max-minutes", "1440"), 0, self.err)


class ConfigTest(Base):
    def setUp(self):
        super().setUp()
        self.make_ranking()
        self.tracks()

    def test_exclude_by_normalised_name_applies_to_run(self):
        cfg = self.config(f"lastfm_artists:\n  user: {USER}\n  exclude:\n    - BAND   2\n")
        self.assertEqual(self.run_session("--config", str(cfg)), 0, self.err)
        self.assertEqual(self.calls, ["Band 1", "Band 3"])
        self.assertEqual(self.planned_sizes, [2])

    def test_run_needs_no_config_and_uses_the_default_location_when_it_exists(self):
        self.assertEqual(self.run_session(), 0, self.err)
        self.assertEqual(self.calls, NAMES)
        default = self.root / "config" / "cowlic" / "bootstrap.yml"
        default.parent.mkdir(parents=True)
        default.write_text(f"lastfm_artists:\n  user: {USER}\n  exclude:\n    - Band 3\n")
        self.calls.clear()
        self.make_ranking(["Band 1", "Band 3", "Band 4"])
        self.tracks(names=["Band 4"])
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, ["Band 4"])

    def test_bad_configs_are_exit_2_before_any_work(self):
        for label, body in (("unknown key", "lastfm_artists:\n  user: abc\n  nope: 1\n"),
                            ("no section", "# nothing\n"),
                            ("playlists too", "- https://open.spotify.com/playlist/" + "a" * 22 + "\nlastfm_artists:\n  user: abc\n"),
                            ("followed section", "followed_artists:\n  enabled: true\nlastfm_artists:\n  user: abc\n"),
                            ("bad years", "lastfm_artists:\n  user: abc\n  years: 99\n")):
            with self.subTest(label):
                self.assertEqual(self.run_session("--config", str(self.config(body))), 2, self.err)
                self.assertEqual(self.backend_calls, 0)
        self.assertEqual(self.run_session("--config", str(self.root / "missing.yml")), 2)

    def test_the_user_name_and_key_appear_in_no_file_and_no_output(self):
        cfg = self.config()
        self.main("rank", "--config", str(cfg), "--refresh")
        self.run_session("--config", str(cfg), "--result-json", str(self.root / "r.json"))
        self.main("plan", "--max-new", "3", "--max-minutes", "5", "--config", str(cfg))
        self.status("--config", str(cfg))
        for path in self.dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(USER.encode(), path.read_bytes(), path.name)
                self.assertNotIn(KEY.encode(), path.read_bytes(), path.name)
        self.assertNotIn(USER, self.out + self.err)
        self.assertNotIn(KEY, self.out + self.err)


class CrashTest(Base):
    def test_a_killed_session_loses_at_most_the_current_track_and_resumes(self):
        self.make_ranking(["Band 1", "Band 2"])
        self.tracks(names=["Band 1", "Band 2"])
        inner = self.deps.download

        def die_in_band_2(song, m, tdir):
            if song["artists"][0] == "Band 2":
                raise Boom()
            return inner(song, m, tdir)
        self.deps.download = die_in_band_2
        with self.assertRaises(Boom):
            self.run_session()
        self.assertEqual(self.entry("Band 1")["status"], "done")  # written before the crash, valid JSON
        self.assertNotIn(ingest.lastfm_artist_key("Band 2"), self.progress()["artists"])
        self.assertEqual(len(self.f.uploaded), 2)
        self.deps.download = inner
        self.calls.clear()
        self.assertEqual(self.run_session(), 0)
        self.assertEqual(self.calls, ["Band 2"])
        self.assertEqual(len(self.f.uploaded), 4)
        self.assertEqual(len(set(self.f.uploaded)), 4)
        self.assertEqual(self.status(), ["done: 2 artists, 4 tracks"])


class GuardArithmeticTest(unittest.TestCase):
    def test_next_day_and_fit(self):
        msg = bootstrap.overlap_problem(datetime.datetime(2026, 10, 11, 20, 0, 0), 60)
        self.assertIn("at most 58 minutes fit", msg)
        self.assertIsNone(bootstrap.overlap_problem(datetime.datetime(2026, 10, 11, 22, 0, 0), 1000))
        self.assertIsNotNone(bootstrap.overlap_problem(datetime.datetime(2026, 10, 11, 22, 0, 0), 1440))
        self.assertIn("at most 0 minutes", bootstrap.overlap_problem(datetime.datetime(2026, 10, 11, 21, 10, 30), 1))


if __name__ == "__main__":
    unittest.main()

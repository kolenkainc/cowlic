"""Tests for what the bootstrap needs from run_lastfm (#166): an injected ranking and its own state file, the
per-artist hook with the enumeration status, the rescan_days override, the session-sized Last.fm budget. Everything is
faked: no Last.fm, no YouTube, no Spotify, no network."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ingest
import lastfm
import lastfm_tracks

from test_ingest import Fakes, FixedToday
import test_lastfm_source as tls
from test_lastfm_source import KEY, NoNetwork, lf_deps, lrec, rank
import test_lastfm_tracks as tlt


def at(records, status="ok", truncated=False):
    return lastfm_tracks.ArtistTracks(records=records, status=status, albums_truncated=truncated)


class HookTest(NoNetwork):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "work").mkdir()
        self.path = self.dir / "state" / "ingest-state.json"
        self.today = FixedToday("2026-10-10")
        self.records: dict = {}
        self.calls: list = []
        self.existing: set = set()
        self.stored: list = []
        self.f = Fakes(self.dir)

    enum = tls.FlowTest.enum
    state = tls.FlowTest.state

    def go(self, artists, cfg=None, limits=None, dry=False, deps=None, state=None, **kw):
        self.hooked = []
        lr = ingest.LastfmRun()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                ingest.run_lastfm(lr, cfg or ingest.LastfmConfig(user="u1"), artists, self.enum, self.stored,
                                  deps or lf_deps(self.f), self.existing, {}, self.dir / "work", dry, limits,
                                  state or self.state(), on_artist=lambda a, r: self.hooked.append((a["name"], r)), **kw)
        finally:
            self.out = out.getvalue()
        return lr

    def reports(self):
        return [(n, r.outcome, r.enum, r.truncated, r.reason) for n, r in self.hooked]

    # --- the hook ---

    def test_done_artists_reach_the_hook_in_order_with_their_counts_and_key(self):
        self.records = {"Band 1": at([lrec("Band 1", "a"), lrec("Band 1", "b")]), "Band 2": at([lrec("Band 2", "c")])}
        self.go([rank(1), rank(2)])
        self.assertEqual(self.reports(), [("Band 1", "done", "ok", False, None), ("Band 2", "done", "ok", False, None)])
        self.assertEqual([r.counts["new"] for _, r in self.hooked], [2, 1])
        self.assertEqual(self.hooked[0][1].key, ingest.lastfm_artist_key("Band 1"))

    def test_the_hook_runs_after_the_engine_state_write(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")])}
        seen = []
        with contextlib.redirect_stdout(io.StringIO()):
            ingest.run_lastfm(ingest.LastfmRun(), ingest.LastfmConfig(user="u1"), [rank(1)], self.enum, [],
                              lf_deps(self.f), self.existing, {}, self.dir / "work", False, None, self.state(),
                              on_artist=lambda a, r: seen.append(self.state().artists.keys()))
        self.assertEqual([set(s) for s in seen], [{ingest.lastfm_artist_key("Band 1")}])

    def test_unknown_artist_no_albums_and_truncated_are_statuses_not_failures(self):
        self.records = {"Band 1": at([], "unknown_artist"), "Band 2": at([], "no_albums"),
                        "Band 3": at([lrec("Band 3", "c")], "ok", True)}
        lr = self.go([rank(1), rank(2), rank(3)])
        self.assertEqual(self.reports(), [("Band 1", "done", "unknown_artist", False, None),
                                          ("Band 2", "done", "no_albums", False, None),
                                          ("Band 3", "done", "ok", True, None)])
        self.assertEqual((lr.failed, lr.scanned), (0, 3))  # as on master: scanned, recorded in the engine state

    def test_an_unusable_name_reaches_the_hook_before_enumeration(self):
        self.records = {"Band 2": at([lrec("Band 2", "c")])}
        lr = self.go([rank(1, "!!!"), rank(2)])
        self.assertEqual(lr.unusable, 1)
        self.assertEqual(self.calls, ["Band 2"])
        self.assertEqual(self.reports()[0], ("!!!", "done", "unusable_artist", False, None))
        self.assertEqual(self.hooked[0][1].counts["new"], 0)

    def test_a_failed_enumeration_reaches_the_hook_as_failed_and_the_others_go_on(self):
        self.records = {"Band 1": lastfm.LastfmError("Last.fm kept answering HTTP 500 after 3 retries"),
                        "Band 2": lastfm_tracks.ArtistBudgetExceeded("Last.fm call limit reached"),
                        "Band 3": at([lrec("Band 3", "c")])}
        lr = self.go([rank(1), rank(2), rank(3)])
        self.assertEqual(self.reports(), [("Band 1", "failed", None, False, None), ("Band 2", "failed", None, False, None),
                                          ("Band 3", "done", "ok", False, None)])
        self.assertEqual((lr.failed, lr.scanned), (2, 1))

    def test_a_cap_stop_is_cut_with_the_partial_counts(self):
        self.records = {"Band 1": at([lrec("Band 1", "a"), lrec("Band 1", "b")], "ok", True), "Band 2": at([lrec("Band 2", "c")])}
        self.go([rank(1), rank(2)], limits=ingest.RunLimits(max_new=1))
        self.assertEqual(self.reports(), [("Band 1", "cut", "ok", True, "cap")])
        self.assertEqual(self.hooked[0][1].counts["new"], 1)

    def test_a_time_stop_is_cut(self):
        now = [0.0]
        limits = ingest.RunLimits(max_minutes=1, clock=lambda: now[0])
        self.records = {"Band 1": at([lrec("Band 1", "a"), lrec("Band 1", "b")])}
        deps = lf_deps(self.f)
        inner = deps.download

        def slow(s, m, t):
            now[0] += 120
            return inner(s, m, t)
        deps.download = slow
        self.go([rank(1)], limits=limits, deps=deps)
        self.assertEqual(self.reports(), [("Band 1", "cut", "ok", False, "time")])

    def test_the_sign_in_stop_is_cut_and_the_hook_runs_before_it_is_raised(self):
        self.records = {"Band 1": at([lrec("Band 1", "a"), lrec("Band 1", "b")], "ok", True), "Band 2": at([lrec("Band 2", "c")])}
        deps = lf_deps(self.f)
        inner = deps.download
        n = []

        def download(s, m, t):
            n.append(1)
            if len(n) == 2:
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)")
            return inner(s, m, t)
        deps.download = download
        with self.assertRaises(ingest.YouTubeSignInRequired):
            self.go([rank(1), rank(2)], deps=deps)
        self.assertEqual(self.reports(), [("Band 1", "cut", "ok", True, "sign-in")])
        self.assertEqual(self.hooked[0][1].counts["new"], 1)
        self.assertEqual(self.calls, ["Band 1"])

    def test_a_plain_list_enumerator_counts_as_status_ok(self):
        self.records = {"Band 1": [lrec("Band 1", "a")]}
        self.go([rank(1)])
        self.assertEqual(self.reports(), [("Band 1", "done", "ok", False, None)])

    def test_no_hook_gives_the_same_log_and_counters_as_a_hook(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")]), "Band 2": at([], "unknown_artist")}
        with_hook = self.go([rank(1), rank(2)])
        out_hook = self.out
        self.setUp()
        self.records = {"Band 1": at([lrec("Band 1", "a")]), "Band 2": at([], "unknown_artist")}
        lr = ingest.LastfmRun()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ingest.run_lastfm(lr, ingest.LastfmConfig(user="u1"), [rank(1), rank(2)], self.enum, [], lf_deps(self.f),
                              self.existing, {}, self.dir / "work", False, None, self.state())
        self.assertEqual((buf.getvalue(), lr), (out_hook, with_hook))

    def test_the_hook_output_holds_no_names_in_the_log(self):
        self.records = {"Band 1": at([lrec("Band 1", "Secret Title Alpha")])}
        self.go([rank(1)])
        self.assertNotIn(ingest.lastfm_artist_key("Band 1"), self.out)

    # --- injected ranking, its own state file, rescan_days ---

    def test_a_separate_state_file_leaves_the_nightly_one_untouched_byte_for_byte(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")])}
        nightly = self.dir / "state" / "ingest-state.json"
        nightly.parent.mkdir()
        nightly.write_bytes(b'{"version": 1, "tracks": {}}\n')
        before = nightly.read_bytes()
        own = self.dir / "state" / "bootstrap-state.json"
        self.go([rank(1)], state=ingest.RunState.load(own, None, self.today))
        self.assertEqual(nightly.read_bytes(), before)
        self.assertEqual(set(json.loads(own.read_text())["artists"]), {ingest.lastfm_artist_key("Band 1")})

    def test_an_injected_ranking_is_used_and_no_ranking_call_is_made(self):
        self.records = {"Band 9": at([lrec("Band 9", "z")])}
        with mock.patch.object(lastfm, "top_artists", side_effect=AssertionError("ranking fetched")):
            self.go([rank(9)])
        self.assertEqual(self.calls, ["Band 9"])

    def test_rescan_days_zero_works_the_callers_list_whatever_the_engine_scan_dates_say(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")])}
        self.go([rank(1)])                       # scanned today (state written)
        self.calls.clear()
        lr = self.go([rank(1)])                  # default 7 days: skipped
        self.assertEqual((self.calls, lr.recent), ([], 1))
        lr = self.go([rank(1)], rescan_days=0)   # override: worked although the engine scanned it today
        self.assertEqual((self.calls, lr.recent, lr.scanned), (["Band 1"], 0, 1))

    def test_the_override_beats_the_config_in_both_directions(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")])}
        cfg0 = ingest.LastfmConfig(user="u1", rescan_days=0)
        self.go([rank(1)], cfg0)
        self.calls.clear()
        self.go([rank(1)], cfg0, rescan_days=30)
        self.assertEqual(self.calls, [])

    def test_a_bad_rescan_days_is_refused(self):
        for bad in (-1, True, 1.5, "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.go([rank(1)], rescan_days=bad)

    def test_dry_run_with_an_injected_ranking_writes_no_state_but_calls_the_hook(self):
        self.records = {"Band 1": at([lrec("Band 1", "a")])}
        self.go([rank(1)], dry=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.reports(), [("Band 1", "done", "ok", False, None)])
        self.assertEqual(self.hooked[0][1].counts["dry_run"], 1)

    def test_resume_after_a_stop_with_an_injected_ranking_and_zero_rescan(self):
        self.records = {"Band 1": at([lrec("Band 1", "a"), lrec("Band 1", "b")]), "Band 2": at([lrec("Band 2", "c")])}
        self.go([rank(1), rank(2)], limits=ingest.RunLimits(max_new=1), rescan_days=0)
        self.calls.clear()
        lr = self.go([rank(1), rank(2)], limits=ingest.RunLimits(max_new=5), rescan_days=0)
        self.assertEqual((lr.status, lr.scanned, lr.counts["new"], lr.counts["exists"]), ("done", 2, 2, 1))
        self.assertEqual(len(self.f.uploaded), 3)


class RealEnumeratorTest(NoNetwork):
    """real_lastfm_enumerator passes the status through when asked; the default is unchanged."""

    def client(self, fake):
        clk = tlt.Clock()
        return lastfm.LastfmClient(tlt.KEY, fetch=fake, sleep=clk.sleep, clock=clk.now, out=lambda s: None)

    def enumerate(self, fake, name="Fake Artist", full=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return ingest.real_lastfm_enumerator(self.client(fake), full=full)({"name": name, "mbid": None})

    def test_unknown_artist_and_no_albums_are_statuses(self):
        res = self.enumerate(tlt.Fake([], {}, top_error=tlt.error(6)))
        self.assertEqual((res.status, res.records), ("unknown_artist", []))
        res = self.enumerate(tlt.Fake([tlt.top_albums([])], {}))
        self.assertEqual((res.status, res.records), ("no_albums", []))

    def test_an_unusable_name_is_a_status_without_a_call(self):
        fake = tlt.Fake([], {})
        self.assertEqual(self.enumerate(fake, "!!!").status, "unusable_artist")
        self.assertEqual(fake.queries, [])

    def test_more_than_max_albums_is_truncated(self):
        names = [f"Album {i}" for i in range(lastfm_tracks.MAX_ALBUMS + 1)]
        infos = {n: tlt.album_info(n, [tlt.tr("T " + n, "200", 1)]) for n in names}
        half = lastfm_tracks.PAGE_LIMIT
        pages = [tlt.top_albums(names[i:i + half], i // half + 1, -(-len(names) // half))
                 for i in range(0, len(names), half)]
        res = self.enumerate(tlt.Fake(pages, infos))
        self.assertEqual((res.status, res.albums_truncated, res.albums), ("ok", True, lastfm_tracks.MAX_ALBUMS))

    def test_a_budget_stop_and_other_errors_still_raise(self):
        with self.assertRaises(lastfm.LastfmError):
            self.enumerate(tlt.Fake([], {}, top_error=tlt.error(10)))

    def test_the_default_still_returns_only_the_records(self):
        res = self.enumerate(tlt.Fake([tlt.top_albums([])], {}), full=False)
        self.assertEqual(res, [])


class SessionBudgetTest(NoNetwork):
    """lastfm_session_client: 150 x planned artists (+ the ranking calls), never the default of 400."""

    def exhaust(self, planned, ranking_calls=0):
        n = []

        def fetch(url, timeout):
            n.append(1)
            return 200, {"ok": 1}
        client = ingest.lastfm_session_client(KEY, planned, ranking_calls, fetch=fetch, sleep=lambda s: None,
                                              min_interval=0, out=lambda s: None)
        with self.assertRaises(lastfm.CallLimitReached):
            while True:
                client.call("album.getInfo")
        return len(n)

    def test_five_artists(self):
        self.assertEqual(self.exhaust(5), 750)

    def test_fifty_artists_and_the_ranking(self):
        self.assertEqual(self.exhaust(50), 7500)
        self.assertEqual(self.exhaust(50, 261), 7761)

    def test_the_default_is_not_used_and_refusals_are_one_clear_line(self):
        self.assertGreater(self.exhaust(5), lastfm.MAX_CALLS)
        for planned, ranking in ((0, 0), (-3, 0), (True, 0), (2.0, 0), (3, -1)):
            with self.subTest(planned=planned, ranking=ranking), self.assertRaises(ingest.IngestError) as cm:
                ingest.lastfm_session_client(KEY, planned, ranking)
            self.assertNotIn("\n", str(cm.exception))
            self.assertNotIn(KEY, str(cm.exception))

    def test_a_bad_key_is_refused_without_echoing_it(self):
        with self.assertRaises(lastfm.LastfmError) as cm:
            ingest.lastfm_session_client("short", 5)
        self.assertNotIn("short", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

"""Tests for the missing-release detector (#171). A fake source, fake stored rows, an injected clock: no YouTube, no
D1, no network (NoNetwork makes a forgotten patch fail fast)."""
import copy
import json
import os
import socket
import stat
import tempfile
import unittest
import urllib.request
from datetime import date
from pathlib import Path
from unittest import mock

import ingest
import lastfm_tracks
import new_releases as nr
from match import normalize

TODAY = date(2026, 10, 12)
BAND = "Radiohead"
HTTP_429 = type("YTMusicServerError", (Exception,), {})


class NoNetwork(unittest.TestCase):
    def setUp(self):
        super().setUp()

        def refuse(*a, **k):
            raise AssertionError("a test reached the network")
        for target, name in ((urllib.request, "urlopen"), (socket.socket, "connect")):
            p = mock.patch.object(target, name, refuse)
            p.start()
            self.addCleanup(p.stop)


def card(title, year, bid):
    return {"title": title, "year": year, "browseId": bid, "type": "Album"}


def track(title, secs=200, artists=(BAND,), **kw):
    t = {"videoId": "vid", "title": title, "artists": [{"name": a, "id": "x"} for a in artists], "duration_seconds": secs,
         "trackNumber": 0, "isExplicit": False}
    t.update(kw)
    return t


NOTSET = object()


class FakeSource:
    """Records every call; answers from dicts. `fail` maps a method name to an exception to raise."""

    def __init__(self, found=NOTSET, albums=None, singles=None, details=None, fail=None, inline=False):
        self.found = found if found is not NOTSET else [{"resultType": "artist", "artist": BAND, "browseId": "UCband"}]
        self.albums = albums if albums is not None else []
        self.singles = singles if singles is not None else []
        self.details = details or {}
        self.fail = fail or {}
        self.inline = inline
        self.calls = []

    def _go(self, name, *a, **k):
        self.calls.append((name, a, k))
        if name in self.fail:
            raise self.fail[name]

    def search(self, query, filter=None, limit=20):
        self._go("search", query, filter=filter, limit=limit)
        return self.found

    def get_artist(self, channelId):
        self._go("get_artist", channelId)
        if self.inline:
            return {"name": BAND, "albums": {"results": self.albums}, "singles": {"results": self.singles}}
        return {"name": BAND, "albums": {"browseId": "UCband", "params": "PA", "results": []},
                "singles": {"browseId": "UCband", "params": "PS", "results": []}}

    def get_artist_albums(self, channelId, params, limit=100, order=None):
        self._go("get_artist_albums", channelId, params, limit=limit, order=order)
        return self.albums if params == "PA" else self.singles

    def get_album(self, browseId):
        self._go("get_album", browseId)
        return self.details[browseId]

    def names(self):
        return [c[0] for c in self.calls]


def instant():
    """(pacer, sleeps): a clock that only moves when the pacer sleeps."""
    t = [1000.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    return nr.Pacer(1.0, lambda: t[0], sleep), sleeps


def run(source, state=None, rows=(), albums=(), **kw):
    state = state if state is not None else nr.empty_state()
    out = []
    kw.setdefault("pacer", instant()[0])
    res = nr.check_artist(source, BAND, state, rows, albums, today=TODAY, out=out.append, **kw)
    return res, state, out


class Found(NoNetwork):
    def test_new_release_found(self):
        src = FakeSource(albums=[card("New Album", "2026", "MPREnew")],
                         details={"MPREnew": {"title": "New Album", "year": "2026",
                                              "tracks": [track("Alpha", 201), track("Beta", 180, artists=(BAND, "Guest"))]}})
        res, state, out = run(src, albums=[("OK Computer", 1997)])
        self.assertEqual(res.status, nr.OK)
        self.assertEqual([r["name"] for r in res.records], ["Alpha", "Beta"])
        r = res.records[1]
        self.assertEqual(r["song_id"], lastfm_tracks.song_id(BAND, "Beta"))  # the id of the Last.fm bootstrap
        self.assertEqual((r["artists"], r["album_artist"], r["album_name"], r["year"], r["duration"], r["album_type"]),
                         ([BAND, "Guest"], BAND, "New Album", 2026, 180, "album"))
        self.assertIsNone(r["track_number"])  # the source gives 0: never invented
        self.assertEqual(res.missing, 1)
        self.assertEqual(len(res.pending), 1)
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual((e["browse_id"], e["checked_at"], e["failures"]), ("UCband", "2026-10-12", 0))
        self.assertEqual(e["evaluated"], [])  # not evaluated until the caller reports the tracks

    def test_record_shape_equals_lastfm_record(self):
        src = FakeSource(albums=[card("A", "2026", "MPREa")],
                         details={"MPREa": {"title": "A", "year": "2026", "tracks": [track("T", 200)]}})
        res, _, _ = run(src)
        ref = lastfm_tracks.make_record({"name": "T", "duration": "200"}, BAND, "A")
        self.assertEqual(set(res.records[0]), set(ref))

    def test_single_is_a_missing_release(self):
        src = FakeSource(singles=[{"title": "Lone", "year": "2026", "browseId": "MPREs", "type": "Single"}],
                         details={"MPREs": {"title": "Lone", "tracks": [track("Lone", 210)]}})
        res, _, _ = run(src)
        self.assertEqual([r["album_type"] for r in res.records], ["single"])
        self.assertEqual(res.records[0]["year"], 2026)  # the album answer has no year: the card's year

    def test_inline_lists_cost_no_albums_call(self):
        src = FakeSource(albums=[card("A", "2026", "MPREa")], inline=True,
                         details={"MPREa": {"title": "A", "tracks": [track("T")]}})
        res, _, _ = run(src)
        self.assertEqual(src.names(), ["search", "get_artist", "get_album"])
        self.assertEqual(len(res.records), 1)

    def test_first_page_only_recency(self):
        src = FakeSource()
        run(src)
        calls = [c for c in src.calls if c[0] == "get_artist_albums"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[2] == {"limit": 1, "order": "Recency"} for c in calls))
        self.assertEqual(src.calls[0][2], {"filter": "artists", "limit": 10})


class SetDifference(NoNetwork):
    def test_already_stored_with_punctuation_difference(self):
        yt = "Hail to the Thief (Live Recordings 2003-2009)"
        d1 = "Hail to the Thief: Live Recordings 2003–2009"
        self.assertEqual(normalize(yt), normalize(d1))
        src = FakeSource(albums=[card(yt, "2025", "MPREh")],
                         details={"MPREh": {"title": yt, "tracks": [track("Alpha")]}})
        res, state, _ = run(src, rows=[("Alpha", BAND, 200)], albums=[(d1, 2025)])
        self.assertEqual((res.records, res.missing, res.in_library), ([], 1, 1))
        self.assertIn("get_album", src.names())
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["evaluated"], [nr.release_key(yt, 2025)])

    def test_unevaluated_partial_album_is_retried_even_with_a_newer_stored_album(self):
        src = FakeSource(albums=[card("Partial", "2023", "MPREp")],
                         details={"MPREp": {"title": "Partial", "tracks": [track("Alpha"), track("Beta")]}})
        res, _, _ = run(src, rows=[("Alpha", BAND, 200)], albums=[("Partial", 2023), ("Newer", 2026)])
        self.assertEqual([r["name"] for r in res.records], ["Beta"])
        self.assertEqual((res.skipped_old, res.in_library), (0, 1))

    def test_evaluated_release_is_not_refetched(self):
        key = nr.release_key("New Album", 2026)
        state = nr.empty_state()
        state["artists"][nr.artist_key(BAND)] = {"browse_id": "UCband", "evaluated": [key]}
        src = FakeSource(albums=[card("New Album", "2026", "MPREn")])
        res, _, _ = run(src, state=state, force=True)
        self.assertNotIn("get_album", src.names())
        self.assertEqual((res.evaluated_hits, res.missing), (1, 0))

    def test_old_release_skipped_undated_kept(self):
        src = FakeSource(albums=[card("Old", "1999", "MPREo"), {"title": "NoYear", "browseId": "MPREu"},
                                 card("Recent", "2025", "MPREr")],
                         details={"MPREu": {"title": "NoYear", "tracks": []}, "MPREr": {"title": "Recent", "tracks": []}})
        res, _, _ = run(src, albums=[("Kid A", 2000), ("X", 2024)])
        self.assertEqual(res.skipped_old, 1)
        self.assertEqual(sorted(c[1][0] for c in src.calls if c[0] == "get_album"), ["MPREr", "MPREu"])
        # dated first, undated after
        self.assertEqual([c[1][0] for c in src.calls if c[0] == "get_album"], ["MPREr", "MPREu"])

    def test_no_stored_album_means_no_year_skip(self):
        src = FakeSource(albums=[card("Old", "1999", "MPREo")], details={"MPREo": {"title": "Old", "tracks": []}})
        res, _, _ = run(src)
        self.assertEqual((res.skipped_old, res.missing), (0, 1))

    def test_dedupe_drops_stored_tracks_and_evaluates_empty_release(self):
        src = FakeSource(albums=[card("Different Title Edition", "2026", "MPREd")],
                         details={"MPREd": {"title": "Different Title Edition", "tracks": [track("Alpha", 201)]}})
        res, state, _ = run(src, rows=[("Alpha", BAND, 200)])
        self.assertEqual((res.records, res.dropped_stored, res.pending), ([], 1, []))
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual(e["evaluated"], [nr.release_key("Different Title Edition", 2026)])

    def test_compilation_edge_known_limitation(self):
        # the owner holds the songs under a compilation title: the release looks missing (one get_album), the tracks
        # are dropped by artist + title + duration, the key is evaluated so it is not fetched again.
        src = FakeSource(albums=[card("Greatest Hits", "2026", "MPREg")],
                         details={"MPREg": {"title": "Greatest Hits", "tracks": [track("Alpha", 200)]}})
        res, state, _ = run(src, rows=[("Alpha", BAND, 200)], albums=[("Hits Compilation", 2020)])
        self.assertEqual((res.missing, res.records), (1, []))
        self.assertEqual(len(state["artists"][nr.artist_key(BAND)]["evaluated"]), 1)

    def test_foreign_track_dropped(self):
        src = FakeSource(albums=[card("Comp", "2026", "MPREc")],
                         details={"MPREc": {"title": "Comp", "tracks": [track("Mine"), track("Theirs", artists=("Other",))]}})
        res, _, _ = run(src)
        self.assertEqual([r["name"] for r in res.records], ["Mine"])

    def test_same_song_on_album_and_single_once(self):
        src = FakeSource(albums=[card("A", "2026", "MPREa")], singles=[card("S", "2026", "MPREs")],
                         details={"MPREa": {"tracks": [track("Same")]}, "MPREs": {"tracks": [track("Same")]}})
        res, _, _ = run(src)
        self.assertEqual(len(res.records), 1)
        self.assertEqual(len(res.pending), 2)


class Outcomes(NoNetwork):
    def setUp(self):
        super().setUp()
        self.src = FakeSource(albums=[card("A", "2026", "MPREa"), card("B", "2026", "MPREb")],
                              details={"MPREa": {"tracks": [track("a1"), track("a2")]},
                                       "MPREb": {"tracks": [track("b1")]}})

    def test_failed_track_blocks_evaluation(self):
        res, state, _ = run(self.src)
        ids = {r["name"]: r["song_id"] for r in res.records}
        added = nr.mark_outcomes(state, res, {ids["a1"]: "stored", ids["a2"]: "failed", ids["b1"]: "no_match"})
        self.assertEqual(added, 1)
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["evaluated"], [nr.release_key("B", 2026)])

    def test_cut_by_cap_is_not_evaluated(self):
        res, state, _ = run(self.src)
        ids = {r["name"]: r["song_id"] for r in res.records}
        added = nr.mark_outcomes(state, res, {ids["a1"]: "stored", ids["b1"]: "too_big"})  # a2 never reached
        self.assertEqual(added, 1)
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["evaluated"], [nr.release_key("B", 2026)])

    def test_all_handled_statuses_evaluate(self):
        res, state, _ = run(self.src)
        statuses = {r["song_id"]: s for r, s in zip(res.records, ["stored", "present", "dropped", "no_match"])}
        self.assertEqual(nr.mark_outcomes(state, res, statuses), 2)

    def test_evaluated_cap_evicts_oldest(self):
        keys = [f"{i:016x}:" for i in range(60)]
        ev = []
        for k in keys:
            ev = nr.add_evaluated(ev, k)
        self.assertEqual((len(ev), ev[0], ev[-1]), (50, keys[10], keys[59]))
        self.assertEqual(nr.add_evaluated(ev, keys[10])[-1], keys[10])  # present: moves to the end, no duplicate
        self.assertEqual(len(nr.add_evaluated(ev, keys[10])), 50)


class Artist(NoNetwork):
    def test_ambiguous_artist_skipped_and_not_failed(self):
        src = FakeSource(found=[{"resultType": "artist", "artist": BAND, "browseId": "UC1"},
                                {"resultType": "artist", "artist": BAND.upper(), "browseId": "UC2"}])
        res, state, out = run(src)
        self.assertEqual((res.status, src.names()), (nr.AMBIGUOUS, ["search"]))
        e = state["artists"][nr.artist_key(BAND)]
        self.assertNotIn("browse_id", e)
        self.assertEqual(e["failures"], 0)
        again = nr.check_artist(src, BAND, state, [], [], today=TODAY, out=out.append)  # not due: no calls at all
        self.assertEqual((again.status, src.names()), (nr.NOT_DUE, ["search"]))

    def test_near_name_is_not_accepted(self):
        src = FakeSource(found=[{"resultType": "artist", "artist": "Radiohead Tribute", "browseId": "UC1"}])
        res, _, _ = run(src)
        self.assertEqual(res.status, nr.UNRESOLVED)

    def test_same_id_twice_is_not_ambiguous(self):
        status, bid = nr.resolve_browse_id([{"artist": BAND, "browseId": "UC1"}, {"artist": BAND, "browseId": "UC1"}], BAND)
        self.assertEqual((status, bid), (nr.OK, "UC1"))

    def test_cached_browse_id_skips_search(self):
        state = nr.empty_state()
        state["artists"][nr.artist_key(BAND)] = {"browse_id": "UCband"}
        src = FakeSource()
        run(src, state=state)
        self.assertNotIn("search", src.names())

    def test_unusable_name(self):
        res = nr.check_artist(FakeSource(), "!!!", nr.empty_state(), [], [], today=TODAY, out=lambda s: None)
        self.assertEqual(res.status, nr.UNUSABLE)

    def test_key_equals_engine_key(self):
        for name in ("Radiohead", "AC/DC", "Мумий Тролль", "  Beyoncé "):
            self.assertEqual(nr.artist_key(name), ingest.lastfm_artist_key(name))


class Due(NoNetwork):
    def test_is_due(self):
        self.assertTrue(nr.is_due(None, TODAY))
        self.assertTrue(nr.is_due({}, TODAY))
        self.assertFalse(nr.is_due({"checked_at": "2026-10-06"}, TODAY))  # 6 days
        self.assertTrue(nr.is_due({"checked_at": "2026-10-05"}, TODAY))   # 7 days
        self.assertTrue(nr.is_due({"checked_at": "2027-01-01"}, TODAY))   # in the future: due
        self.assertFalse(nr.is_due({"checked_at": "2026-10-10"}, TODAY, recheck_days=3))

    def test_not_due_makes_no_call(self):
        state = nr.empty_state()
        state["artists"][nr.artist_key(BAND)] = {"browse_id": "UCband", "checked_at": "2026-10-11"}
        src = FakeSource()
        res, _, _ = run(src, state=state)
        self.assertEqual((res.status, src.calls), (nr.NOT_DUE, []))

    def test_select_due_order_and_cap(self):
        state = nr.empty_state()
        state["artists"][nr.artist_key("B")] = {"checked_at": "2026-09-01"}
        state["artists"][nr.artist_key("C")] = {"checked_at": "2026-08-01"}
        state["artists"][nr.artist_key("D")] = {"checked_at": "2026-10-11"}
        names, total = nr.select_due(["A", "B", "C", "D", "a", "!!!", "E"], state, TODAY, 3)
        self.assertEqual((names, total), (["A", "E", "C"], 4))  # never checked first, then oldest; repeats ignored

    def test_failing_artists_back_off_and_sort_last(self):
        state = nr.empty_state()
        for n in ("F1", "F2", "F3"):
            state["artists"][nr.artist_key(n)] = {"failures": 1, "failed_at": "2026-10-12"}
        self.assertEqual(nr.select_due(["F1", "F2", "F3", "New"], state, TODAY, 10), (["New"], 1))  # backing off
        later = date(2026, 10, 13)  # 1 day after: due again, but behind the healthy ones
        self.assertEqual(nr.select_due(["F1", "F2", "F3", "New"], state, later, 2), (["New", "F1"], 4))
        self.assertEqual([nr.backoff_days(n) for n in (1, 2, 3, 4, 99)], [1, 2, 4, 7, 7])
        self.assertFalse(nr.is_due({"failures": 3, "failed_at": "2026-10-10"}, TODAY))   # 2 days of 4
        self.assertTrue(nr.is_due({"failures": 3, "failed_at": "2026-10-08"}, TODAY))    # 4 days
        self.assertTrue(nr.is_due({"failures": 3, "failed_at": "2027-01-01"}, TODAY))    # future: due

    def test_failures_cannot_starve_the_rest(self):
        state = nr.empty_state()
        for n in ("F1", "F2"):
            state["artists"][nr.artist_key(n)] = {"failures": 5, "failed_at": "2026-10-12"}
        names, _ = nr.select_due(["F1", "F2", "Healthy"], state, TODAY, 1)
        self.assertEqual(names, ["Healthy"])

    def test_success_clears_the_failure_marks(self):
        state = nr.empty_state()
        state["artists"][nr.artist_key(BAND)] = {"browse_id": "UCband", "failures": 2, "failed_at": "2026-10-01"}
        run(FakeSource(), state=state)
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["failures"], 0)
        self.assertNotIn("failed_at", state["artists"][nr.artist_key(BAND)])

    def test_failed_check_keeps_checked_at_and_evaluated(self):
        state = nr.empty_state()
        k = nr.artist_key(BAND)
        state["artists"][k] = {"browse_id": "UCband", "checked_at": "2026-09-01", "failures": 1,
                               "evaluated": ["0123456789abcdef:2020"]}
        before = copy.deepcopy(state)
        src = FakeSource(fail={"get_artist": RuntimeError("boom https://x/Radiohead")})
        res, _, out = run(src, state=state)
        self.assertEqual(res.status, nr.FAILED)
        self.assertEqual(state["artists"][k], {**before["artists"][k], "failures": 2, "failed_at": "2026-10-12"})
        self.assertEqual(len(src.calls), 1)  # no retry

    def test_failure_after_search_keeps_browse_id(self):
        src = FakeSource(fail={"get_artist": RuntimeError("x")})
        res, state, _ = run(src)
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual((e["browse_id"], e["failures"], "checked_at" in e), ("UCband", 1, False))

    def test_failed_get_album_commits_nothing_else(self):
        src = FakeSource(albums=[card("A", "2026", "MPREa"), card("B", "2026", "MPREb")],
                         details={"MPREa": None, "MPREb": None})  # no release can be read at all
        res, state, _ = run(src)
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual((res.status, res.records, "checked_at" in e, e.get("evaluated")), (nr.FAILED, [], False, None))
        self.assertEqual((e["failures"], e["failed_at"]), (1, "2026-10-12"))

    def test_poison_release_does_not_block_the_others(self):
        src = FakeSource(albums=[card("Poison", "2027", "MPREp"), card("Good", "2026", "MPREg")],
                         details={"MPREg": {"tracks": [track("Fine")]}, "MPREp": None})
        res, state, out = run(src)
        self.assertEqual((res.status, [r["name"] for r in res.records], res.release_failures), (nr.OK, ["Fine"], 1))
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual(e["checked_at"], "2026-10-12")
        self.assertNotIn(nr.release_key("Poison", 2027), e["evaluated"])  # retried at the next check
        self.assertIn("1 release errors", out[0])

    def test_poison_release_raising_in_the_source(self):
        class Raising(FakeSource):
            def get_album(self, browseId):
                if browseId == "MPREp":
                    raise KeyError("x")
                return super().get_album(browseId)
        src = Raising(albums=[card("Poison", "2027", "MPREp"), card("Good", "2026", "MPREg")],
                      details={"MPREg": {"tracks": [track("Fine")]}})
        res, _, _ = run(src)
        self.assertEqual((res.status, len(res.records)), (nr.OK, 1))

    def test_refusal_in_get_album_still_stops(self):
        src = FakeSource(albums=[card("A", "2026", "MPREa"), card("B", "2026", "MPREb")],
                         fail={"get_album": HTTP_429("HTTP 429")})
        res, _, _ = run(src)
        self.assertEqual((res.status, src.names().count("get_album")), (nr.REFUSED, 1))

    def test_empty_album_page_is_not_evaluated(self):
        src = FakeSource(albums=[card("Soon", "2026", "MPREs")], details={"MPREs": {"title": "Soon", "tracks": []}})
        res, state, out = run(src)
        e = state["artists"][nr.artist_key(BAND)]
        self.assertEqual((res.status, res.empty_releases, e["evaluated"]), (nr.OK, 1, []))
        self.assertIn("1 empty", out[0])

    def test_foreign_only_release_is_handled_and_counted(self):
        src = FakeSource(albums=[card("Comp", "2026", "MPREc")],
                         details={"MPREc": {"tracks": [track("T1", artists=("Other",)), track("T2", artists=("Third",))]}})
        res, state, out = run(src)
        self.assertEqual((res.records, res.foreign), ([], 2))
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["evaluated"], [nr.release_key("Comp", 2026)])
        self.assertIn("2 foreign", out[0])


class Budget(NoNetwork):
    def test_requests_and_throttle(self):
        pacer, sleeps = instant()
        src = FakeSource(albums=[card(f"A{i}", "2026", f"MPRE{i}") for i in range(8)],
                         details={f"MPRE{i}": {"tracks": [track(f"t{i}")]} for i in range(8)})
        res, _, _ = run(src, pacer=pacer, max_releases=3)
        # search 1 + get_artist 1 + 2 lists x 2 = 6, + 3 get_album
        self.assertEqual((res.requests, src.names().count("get_album"), res.deferred, len(res.records)), (9, 3, 5, 3))
        self.assertEqual(len(sleeps), len(src.calls) - 1)
        self.assertTrue(all(s >= 0.99 for s in sleeps))

    def test_default_pacer_is_shared_across_artists(self):
        t = [5000.0]
        calls = []

        def sleep(sec):
            t[0] += sec

        class Stamp(FakeSource):
            def _go(self, name, *a, **k):
                calls.append(t[0])
                super()._go(name, *a, **k)
        nr._DEFAULT_PACER._last = None
        with mock.patch.object(nr.time, "monotonic", lambda: t[0]), mock.patch.object(nr.time, "sleep", sleep):
            for name in ("Alpha", "Beta"):  # no pacer argument: the default one
                nr.check_artist(Stamp(found=[{"artist": name, "browseId": "UC" + name}]), name, nr.empty_state(), [], [],
                                today=TODAY, out=lambda s: None)
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertGreater(len(gaps), 4)
        self.assertTrue(all(g >= 0.999 for g in gaps), gaps)  # the artist boundary included
        nr._DEFAULT_PACER._last = None

    def test_stored_albums_is_required(self):
        with self.assertRaises(TypeError):
            nr.check_artist(FakeSource(), BAND, nr.empty_state(), [], today=TODAY)

    def test_budget_is_hard(self):
        g = nr.GuardedSource(FakeSource(), instant()[0], 2)
        g.get_artist("UC")
        g.get_artist("UC")
        with self.assertRaises(nr.BudgetExceeded):
            g.get_artist("UC")

    def test_albums_call_costs_two(self):
        g = nr.GuardedSource(FakeSource(), instant()[0], 5)
        g.get_artist("UC")
        g.get_artist_albums("UC", "PA")
        g.get_artist_albums("UC", "PS")
        self.assertEqual(g.requests, 5)
        with self.assertRaises(nr.BudgetExceeded):
            g.get_album("MPREx")

    def test_pacer_waits_the_remainder(self):
        t = [0.0]
        sleeps = []
        p = nr.Pacer(1.0, lambda: t[0], lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
        p.wait()
        t[0] += 0.25
        p.wait()
        self.assertEqual(sleeps, [0.75])


class Refusal(NoNetwork):
    def refused(self, exc):
        src = FakeSource(fail={"get_artist": exc})
        res, state, _ = run(src)
        self.assertEqual(res.status, nr.REFUSED, exc)
        self.assertEqual(src.names(), ["search", "get_artist"])  # no retry
        self.assertEqual(state["artists"][nr.artist_key(BAND)]["failures"], 1)

    def test_http_429_403_and_bot_check(self):
        self.refused(HTTP_429("Server returned HTTP 429: Too Many Requests."))
        self.refused(HTTP_429("Server returned HTTP 403: Forbidden"))
        self.refused(RuntimeError("Sign in to confirm you're not a bot"))
        e = RuntimeError("x")
        e.status_code = 429
        self.refused(e)

    def test_other_error_is_failed_not_refused(self):
        res, _, _ = run(FakeSource(fail={"get_artist": KeyError("contents")}))
        self.assertEqual(res.status, nr.FAILED)

    def test_error_text_is_redacted(self):
        g = nr.GuardedSource(FakeSource(fail={"get_artist": RuntimeError("secret Radiohead https://u/x")}), instant()[0], 5)
        with self.assertRaises(nr.SourceError) as cm:
            g.get_artist("UC")
        self.assertNotIn("Radiohead", str(cm.exception))
        self.assertNotIn("https", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__suppress_context__)


class Untrusted(NoNetwork):
    def test_clean_text(self):
        self.assertEqual(nr.clean_text("A\nB\x00C‮  D"), "A B C D")
        for bad in (None, 5, "", "   ", "x" * 301, b"x", ["x"]):
            self.assertIsNone(nr.clean_text(bad))

    def test_parse_releases_bounds_and_types(self):
        raw = [None, 7, {"title": 5, "browseId": "MPREx"}, {"title": "T", "browseId": "bad id"},
               {"title": "T\n\x07", "browseId": "MPREa", "year": "2024"}, {"title": "T", "browseId": "MPREb", "year": "2024"},
               {"title": "!!!", "browseId": "MPREc"}, {"title": "U", "browseId": "MPRED", "year": "20x4"}]
        got = nr.parse_releases(raw, "album")
        self.assertEqual([(r.title, r.year, r.browse_id) for r in got], [("T", 2024, "MPREa"), ("U", None, "MPRED")])
        self.assertEqual(len(nr.parse_releases([{"title": f"t{i}", "browseId": "MPREa"} for i in range(500)], "album")),
                         nr.MAX_PER_KIND)
        for bad in (None, "x", {}, 5):
            self.assertEqual(nr.parse_releases(bad, "album"), [])
        late = [None] * nr.MAX_LISTED + [{"title": "Late", "browseId": "MPREl"}]
        self.assertEqual(nr.parse_releases(late, "album"), [])  # entries beyond MAX_LISTED are never looked at

    def test_repeated_track_title_once_and_other_result_types_ignored(self):
        rel = nr.Release("A", 2026, "MPREa", "album", nr.release_key("A", 2026))
        self.assertEqual(len(nr.build_records(BAND, rel, {"tracks": [track("Same"), track("Same", 300)]})), 1)
        self.assertEqual(nr.resolve_browse_id([{"resultType": "song", "artist": BAND, "browseId": "UC9"},
                                               {"artist": "Else", "browseId": "UC8"}], BAND), (nr.UNRESOLVED, None))

    def test_wrong_shapes_fail_cleanly(self):
        for page in (None, [], "x"):
            src = FakeSource()
            src.get_artist = lambda cid, p=page: p
            res, _, _ = run(src)
            self.assertEqual(res.status, nr.FAILED)

    def test_album_answer_shapes(self):
        rel = nr.Release("A", 2026, "MPREa", "album", nr.release_key("A", 2026))
        for bad in (None, [], {}, {"tracks": "x"}):
            self.assertIsNone(nr.build_records(BAND, rel, bad))
        big = {"tracks": [track(f"t{i}", secs=10**9) for i in range(1000)] + ["x", None, {"title": 5}]}
        recs = nr.build_records(BAND, rel, big)
        self.assertEqual(len(recs), nr.MAX_TRACKS)
        self.assertTrue(all(r["duration"] is None for r in recs))
        t = nr.build_records(BAND, rel, {"tracks": [track("ok\n\x07x", secs=True, trackNumber=10**6, isExplicit="yes")]})[0]
        self.assertEqual((t["name"], t["duration"], t["track_number"], t["explicit"]), ("ok x", None, None, None))

    def test_search_results_shapes(self):
        for bad in (None, "x", {}, {"a": 1}, [], [None, 5, "s"], [{"artist": 5}, {"artist": BAND, "browseId": "bad id"}],
                    [{"resultType": "song", "artist": BAND, "browseId": "UC9"}]):
            with self.assertRaises(nr.SourceError, msg=repr(bad)):
                nr.resolve_browse_id(bad, BAND)

    def test_malformed_search_answer_is_a_failed_check(self):
        for bad in (None, "x", {"a": 1}, [None, 1, "s"], []):
            src = FakeSource(found=bad)
            res, state, _ = run(src)
            e = state["artists"][nr.artist_key(BAND)]
            self.assertEqual(res.status, nr.FAILED, bad)
            self.assertEqual((e["failures"], "checked_at" in e, "browse_id" in e), (1, False, False))
            self.assertEqual(src.names(), ["search"])


class StateFile(NoNetwork):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "state"
        self.path = self.dir / nr.STATE_FILE_NAME

    def good(self):
        s = nr.empty_state()
        s["artists"][nr.artist_key(BAND)] = {"browse_id": "UCband", "checked_at": "2026-10-12", "failures": 0,
                                             "evaluated": ["0a1b2c3d4e5f6071:2025", "99aa88bb77cc66dd:"]}
        return s

    def test_roundtrip_modes_and_no_names(self):
        nr.save_state(self.path, self.good())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(nr.load_state(self.path), self.good())
        self.assertNotIn(BAND, self.path.read_text())
        self.assertEqual(os.listdir(self.dir), [nr.STATE_FILE_NAME])  # no temp file left

    def test_missing_file_is_fresh(self):
        self.assertEqual(nr.load_state(self.path), nr.empty_state())

    def test_atomic_write_keeps_the_old_file_on_failure(self):
        nr.save_state(self.path, self.good())
        before = self.path.read_bytes()
        newer = self.good()
        newer["artists"][nr.artist_key(BAND)]["failures"] = 3
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                nr.save_state(self.path, newer)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.dir), [nr.STATE_FILE_NAME])

    def test_rename_is_used(self):
        with mock.patch("os.replace", wraps=os.replace) as rep, mock.patch("os.fsync", wraps=os.fsync) as fs:
            nr.save_state(self.path, self.good())
        self.assertEqual((rep.call_count, fs.call_count), (1, 1))

    def test_malformed_file_is_a_clear_error_never_empty(self):
        self.dir.mkdir()
        cases = ["", "{", "[]", "null", '{"version": 1}', '{"version": 2, "artists": {}}',
                 '{"version": true, "artists": {}}', '{"version": 1, "artists": [], "x": 1}',
                 '{"version": 1, "artists": {"Radiohead": {}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"extra": 1}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"failures": -1}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"failures": true}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"checked_at": "yesterday"}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"checked_at": "2026-13-45"}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"browse_id": "a b"}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"failed_at": "soon"}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"evaluated": ["x"]}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"evaluated": ["0a1b2c3d4e5f6071:", "0a1b2c3d4e5f6071:"]}}}',
                 '{"version": 1, "artists": {"lfm:0123456789abcdef": {"evaluated": ' + json.dumps([f"{i:016x}:" for i in range(51)]) + '}}}']
        for text in cases:
            self.path.write_text(text)
            with self.assertRaises(nr.NewReleasesStateError, msg=text[:60]):
                nr.load_state(self.path)

    def test_bad_utf8_directory_and_size(self):
        self.dir.mkdir()
        self.path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(nr.NewReleasesStateError):
            nr.load_state(self.path)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(nr.NewReleasesStateError):
            nr.load_state(self.path)
        self.path.rmdir()
        self.path.write_text(" " * (nr.STATE_MAX_BYTES + 1))
        with self.assertRaises(nr.NewReleasesStateError):
            nr.load_state(self.path)

    def test_save_refuses_an_invalid_state(self):
        with self.assertRaises(nr.NewReleasesStateError):
            nr.save_state(self.path, {"version": 1, "artists": {"Radiohead": {}}})
        self.assertFalse(self.path.exists())

    def test_error_text_has_no_content(self):
        self.dir.mkdir()
        self.path.write_text('{"version": 1, "artists": {"Radiohead secret": {}}}')
        with self.assertRaises(nr.NewReleasesStateError) as cm:
            nr.load_state(self.path)
        self.assertNotIn("secret", str(cm.exception))


class Logging(NoNetwork):
    def test_no_name_or_title_in_any_line(self):
        secret_title = "Zzyzx Rarity Album"
        secret_track = "Qwxv Hidden Song"
        src = FakeSource(albums=[card(secret_title, "2026", "MPREz")],
                         details={"MPREz": {"title": secret_title, "tracks": [track(secret_track)]}})
        lines = []
        for fake in (src, FakeSource(fail={"get_artist": RuntimeError(f"{BAND} {secret_title}")}),
                     FakeSource(found=[])):
            state = nr.empty_state()
            nr.check_artist(fake, BAND, state, [], [], today=TODAY, pacer=instant()[0], out=lines.append)
            nr.check_artist(fake, BAND, state, [], [], today=TODAY, pacer=instant()[0], out=lines.append)  # not due / retry
        nr.check_artist(src, "!!!", nr.empty_state(), [], [], today=TODAY, out=lines.append)
        totals = nr.Totals(due=3)
        lines.append(totals.line())
        self.assertGreater(len(lines), 4)
        text = "\n".join(lines).lower()
        for word in ("radiohead", "zzyzx", "qwxv", "http", "mpre", "ucband"):
            self.assertNotIn(word, text)
        self.assertIn(nr.artist_key(BAND), text)

    def test_default_out_is_stderr_only(self):
        import contextlib
        import io
        err, o = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(o):
            nr.check_artist(FakeSource(), BAND, nr.empty_state(), [], [], today=TODAY, pacer=instant()[0])
        self.assertEqual(o.getvalue(), "")
        self.assertNotIn("Radiohead", err.getvalue())

    def test_totals(self):
        t = nr.Totals()
        for status, recs in ((nr.OK, [1]), (nr.OK, []), (nr.FAILED, []), (nr.REFUSED, []), (nr.AMBIGUOUS, []),
                             (nr.NOT_DUE, [])):
            t.add(nr.ArtistCheck(key="k", status=status, records=recs))
        self.assertEqual((t.checked, t.with_new, t.failed, t.skipped), (2, 1, 2, 1))


class Purity(NoNetwork):
    def test_inputs_not_modified(self):
        rows = [("Alpha", BAND, 200)]
        albums = [("X", 2020)]
        before = copy.deepcopy((rows, albums))
        src = FakeSource(albums=[card("N", "2026", "MPREn")], details={"MPREn": {"tracks": [track("Alpha", 200), track("B")]}})
        res, _, _ = run(src, rows=rows, albums=albums)
        self.assertEqual((rows, albums), before)
        self.assertEqual([r["name"] for r in res.records], ["B"])


if __name__ == "__main__":
    unittest.main()

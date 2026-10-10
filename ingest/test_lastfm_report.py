"""Tests for lastfm_report (#153). Offline: fake Last.fm records, fake YouTube Music, fake D1; nothing sleeps."""
import contextlib
import csv
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import lastfm
import lastfm_report as lr
import lastfm_tracks
import match_report as mr
from ingest import IngestError, YouTubeSignInRequired
from test_lastfm_source import KEY, NoNetwork, lrec
from test_match_report import FakeYtm, make_ps, quiet, result


def ranking(n):
    return [{"name": f"Band {i}", "mbid": None, "playcount": 1000 - i} for i in range(1, n + 1)]


def feed(table):
    """enumerate_songs over {artist name: records or an Exception}."""
    def f(artist):
        v = table[artist["name"]]
        if isinstance(v, Exception):
            raise v
        return v
    return f


def do_run(table, ytm, picked=None, max_tracks=150, secrets=(), stored=None, deadline=None, clock=None, **kw):
    rows = []
    ps = make_ps(ytm, **kw)
    picked = picked or [(i, {"name": n, "mbid": None}) for i, n in enumerate(table, 1)]
    extra = {"clock": clock} if clock else {}
    outcomes, stop = lr.run(picked, feed(table), ps, max_tracks, rows, list(secrets), stored, deadline, out=quiet,
                            **extra)
    return rows, outcomes, stop, ps


def rec(artist, title, duration=None):
    return lrec(artist, title, duration)


class MatchModes(NoNetwork):
    def test_modes_are_separate_and_counted(self):
        ytm = FakeYtm({"with dur": [result("With Dur", 201, artist="Band 1")],
                       "no dur": [result("No Dur", 230, artist="Band 1")],            # title-only: length is free
                       "wrong dur": [result("Wrong Dur", 240, artist="Band 1")],      # title ok, duration off
                       "bad title": [result("Other Thing", 200, artist="Band 1")],
                       "live only": [result("Live Only", 200, artist="Zzz Qqq")]})  # artist rejects
        table = {"Band 1": [rec("Band 1", "With Dur", 200), rec("Band 1", "No Dur"), rec("Band 1", "Wrong Dur", 200),
                            rec("Band 1", "Bad Title"), rec("Band 1", "Live Only"), rec("Band 1", "Nothing Found")]}
        rows, outcomes, stop, _ = do_run(table, ytm)
        by = {r.title: r for r in rows}
        self.assertIsNone(stop)
        self.assertEqual((by["With Dur"].status, by["With Dur"].mode), ("matched", "duration"))
        self.assertEqual((by["No Dur"].status, by["No Dur"].mode), ("matched", "title-only"))
        self.assertIsNone(by["No Dur"].delta)
        self.assertEqual(by["No Dur"].matched_artists, "Band 1")
        self.assertEqual((by["Wrong Dur"].status, by["Wrong Dur"].reason, by["Wrong Dur"].mode),
                         ("no_match", "duration", "duration"))
        self.assertEqual((by["Bad Title"].reason, by["Bad Title"].mode), ("title", "title-only"))
        self.assertEqual(by["Live Only"].reason, "artist_variant_or_length")
        self.assertEqual(by["Live Only"].matched_title, "Live Only")
        self.assertEqual(by["Nothing Found"].reason, "no_results")
        self.assertEqual(lr.counts_of(rows, "duration"), {"matched": 1, "no_match": 1, "stored": 0, "error": 0})
        self.assertEqual(lr.counts_of(rows, "title-only"), {"matched": 1, "no_match": 3, "stored": 0, "error": 0})
        self.assertAlmostEqual(lr.rate(lr.counts_of(rows)), 2 / 6)
        text = "\n".join(lr.result_lines(rows))
        self.assertIn("mode duration", text)
        self.assertIn("mode title-only", text)
        self.assertIn("title-only mode is judged on its own wrong rate", text)

    def test_duration_record_never_gets_title_only_treatment(self):
        # a title that only the title-only rule would accept (duration far off) must not match when Last.fm has one
        ytm = FakeYtm({"song": [result("Song", 400, artist="Band 1")]})
        rows, *_ = do_run({"Band 1": [rec("Band 1", "Song", 200)]}, ytm)
        self.assertEqual((rows[0].status, rows[0].mode), ("no_match", "duration"))

    def test_lowest_title_only_listing_ignores_duration_mode_and_sorts(self):
        rows = [lr.Row(1, "A", "x", None, "title-only", "matched", similarity=0.99),
                lr.Row(1, "A", "y", None, "title-only", "matched", similarity=0.94),
                lr.Row(1, "A", "z", 200, "duration", "matched", similarity=0.50),
                lr.Row(1, "A", "w", None, "title-only", "no_match", similarity=0.10)]
        self.assertEqual([r.title for r in lr.lowest_title_only(rows)], ["y", "x"])


class Caps(NoNetwork):
    def table(self, n_artists=2, n=6):
        return {f"Band {a}": [rec(f"Band {a}", f"Song {a}{i}", 200) for i in range(n)] for a in range(1, n_artists + 1)}

    def test_max_tracks_is_a_hard_cap_shared_between_artists(self):
        rows, outcomes, stop, _ = do_run(self.table(3, 10), FakeYtm(), max_tracks=7)
        self.assertEqual(len(rows), 7)
        self.assertEqual([o.sampled for o in outcomes], [3, 2, 2])
        self.assertIsNone(stop)

    def test_max_tracks_below_the_number_of_artists_is_refused(self):
        with self.assertRaises(mr.ReportError):
            do_run(self.table(3, 2), FakeYtm(), max_tracks=2)

    def test_search_budget_stops_the_run(self):
        rows, _, stop, ps = do_run(self.table(1, 6), FakeYtm(), max_searches=4)
        self.assertEqual(stop, "search budget reached")
        self.assertEqual(ps.searches, 4)
        self.assertLess(len(rows), 6)

    def test_time_cap_stops_between_tracks(self):
        ticks = iter([0, 0, 0, 0, 100, 100, 100, 100, 100, 100])
        rows, _, stop, _ = do_run(self.table(1, 6), FakeYtm(), deadline=50, clock=lambda: next(ticks))
        self.assertEqual(stop, "time cap reached")
        self.assertLess(len(rows), 6)
        self.assertGreater(len(rows), 0)

    def test_time_cap_stops_before_the_next_artist(self):
        ticks = iter([0, 0, 999])
        rows, outcomes, stop, _ = do_run(self.table(2, 1), FakeYtm(), deadline=50, clock=lambda: next(ticks))
        self.assertEqual(stop, "time cap reached")
        self.assertEqual(outcomes[1].sampled, 0)

    def test_three_failed_searches_in_a_row_stop(self):
        ytm = FakeYtm(error=ConnectionError("boom"))
        rows, _, stop, _ = do_run(self.table(1, 6), ytm)
        self.assertIn("3 searches failed in a row", stop)
        self.assertEqual([r.status for r in rows], ["error"] * 3)

    def test_sign_in_challenge_stops_at_once(self):
        rows, _, stop, _ = do_run(self.table(1, 6), FakeYtm(error=YouTubeSignInRequired("Sign in to confirm")))
        self.assertIn("not bypassed", stop)
        self.assertEqual(len(rows), 1)

    def test_a_failed_artist_does_not_stop_the_others_and_is_redacted(self):
        table = self.table(2, 2)
        table["Band 1"] = lastfm.LastfmError(f"bad {KEY}")
        rows, outcomes, stop, _ = do_run(table, FakeYtm(), secrets=[KEY])
        self.assertIsNone(stop)
        self.assertIn("LastfmError", outcomes[0].error)
        self.assertNotIn(KEY, outcomes[0].error)
        self.assertEqual(len(rows), 2)

    def test_three_failed_artists_in_a_row_stop(self):
        table = {f"Band {i}": lastfm.LastfmError("down") for i in range(1, 6)}
        _, outcomes, stop, _ = do_run(table, FakeYtm())
        self.assertIn("3 artists failed in a row", stop)
        self.assertEqual(sum(1 for o in outcomes if o.error), 3)

    def test_pick_artists_is_even_deterministic_and_bounded(self):
        picked = lr.pick_artists(ranking(100), 10)
        self.assertEqual([r for r, _ in picked], [1, 11, 21, 31, 41, 51, 61, 71, 81, 91])
        self.assertEqual(picked, lr.pick_artists(ranking(100), 10))
        self.assertEqual([r for r, _ in lr.pick_artists(ranking(3), 10)], [1, 2, 3])
        self.assertEqual(lr.pick_artists([], 5), [])


class Stored(NoNetwork):
    def test_stored_tracks_are_flagged_not_searched(self):
        ytm = FakeYtm({"fresh": [result("Fresh", 200, artist="Band 1")]})
        table = {"Band 1": [rec("Band 1", "Have It", 200), rec("Band 1", "Fresh", 200), rec("Band 1", "No Dur Have")]}
        stored = [("Have It", "Band 1", 201), ("No Dur Have", "Band 1", 180)]
        rows, outcomes, _, ps = do_run(table, ytm, stored=stored)
        st = {r.title: r.status for r in rows}
        self.assertEqual(st, {"Have It": "stored", "Fresh": "matched", "No Dur Have": "stored"})
        self.assertFalse([q for q in ytm.queries if "have" in q.lower()])
        self.assertEqual(outcomes[0].counts["stored"], 2)
        self.assertAlmostEqual(lr.rate(lr.counts_of(rows)), 1.0)  # stored rows are out of the rate

    def test_no_stored_rows_means_no_check(self):
        rows, *_ = do_run({"Band 1": [rec("Band 1", "Have It", 200)]}, FakeYtm())
        self.assertEqual(rows[0].status, "no_match")

    def test_check_skipped_without_d1_variables(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rows, note = lr.stored_check([], reader=lambda: self.fail("must not read"))
        self.assertIsNone(rows)
        self.assertIn("skipped", note)
        self.assertIn("INGEST_CLOUDFLARE_API_TOKEN", note)  # names only

    def test_check_failure_is_a_skip_line_with_redaction(self):
        env = {"CLOUDFLARE_ACCOUNT_ID": "a", "D1_DATABASE_ID": "b", "INGEST_CLOUDFLARE_API_TOKEN": "c"}

        def boom():
            raise IngestError(f"D1 API error 403 {KEY}")
        with mock.patch.dict(os.environ, env, clear=True):
            rows, note = lr.stored_check([KEY], reader=boom)
            ok_rows, ok_note = lr.stored_check([KEY], reader=lambda: [("t", "a", 1)])
        self.assertIsNone(rows)
        self.assertIn("skipped", note)
        self.assertNotIn(KEY, note)
        self.assertEqual(ok_rows, [("t", "a", 1)])
        self.assertIn("read-only SELECT", ok_note)


class Tmp(NoNetwork):
    def tmp(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Path(td.name)


class Csv(Tmp):
    def write(self, rows, name="match-report-lastfm-t.csv"):
        path = self.tmp() / name
        mr.write_csv(path, rows, lr.COLUMNS)
        return path

    def test_columns_and_cells(self):
        row = lr.Row(3, "=cmd|' /C calc'!A0", "+SUM(1)\nline\x07", None, "title-only", "matched", "@x", "-y", 200.0, None,
                     0.97, "", "", "")
        path = self.write([row])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        table = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual(table[0], lr.COLUMNS)
        c = dict(zip(lr.COLUMNS, table[1]))
        self.assertEqual(c["artist_rank"], "3")
        self.assertTrue(c["artist"].startswith("'="))
        self.assertTrue(c["title"].startswith("'+"))
        self.assertNotIn("\x07", c["title"])
        self.assertNotIn("\n", c["title"])
        self.assertTrue(c["matched_title"].startswith("'@"))
        self.assertTrue(c["matched_artists"].startswith("'-"))
        self.assertEqual((c["lastfm_duration_s"], c["mode"], c["status"], c["reviewed"]), ("", "title-only", "matched", ""))
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_the_reviewed_label_cell_is_also_guarded(self):
        row = lr.Row(1, "A", "t", 100, "duration", "matched", reviewed="=1+1")
        c = dict(zip(lr.COLUMNS, row.cells()))
        self.assertEqual(c["reviewed"], "'=1+1")

    def test_no_overwrite_and_name_pattern(self):
        path = self.write([])
        with self.assertRaises(mr.ReportError):
            mr.validate_out(str(path))
        with self.assertRaises(mr.ReportError):
            mr.validate_out("results.csv")
        self.assertRegex(lr.default_out(), r"match-report-lastfm-\d{8}T\d{6}Z\.csv")
        self.assertTrue(mr._REPORT_NAME.fullmatch(lr.default_out()))

    def test_no_key_in_a_written_report(self):
        ytm = FakeYtm({"song": [result("Song", 200, artist="Band 1")]})
        rows, *_ = do_run({"Band 1": [rec("Band 1", "Song", 200)]}, ytm)
        self.assertNotIn(KEY, self.write(rows).read_text(encoding="utf-8-sig"))

    def test_round_trip_keeps_labels_and_refuses_foreign_files(self):
        rows = [lr.Row(1, "A", "t", 200, "duration", "matched", "t", "A", 200.0, 0.0, 1.0),
                lr.Row(2, "B", "u", None, "title-only", "no_match", reason="title", pattern="other")]
        path = self.write(rows)
        back = lr.read_rows(str(path))
        self.assertEqual([(r.rank, r.title, r.mode, r.status, r.duration) for r in back],
                         [(1, "t", "duration", "matched", 200), (2, "u", "title-only", "no_match", None)])
        for bad in ("a,b\n1,2\n", "", ",".join(lr.COLUMNS) + "\n1,2\n"):
            f = self.tmp() / "match-report-lastfm-bad.csv"
            f.write_text(bad, encoding="utf-8")
            with self.assertRaises(mr.ReportError):
                lr.read_rows(str(f))
        with self.assertRaises(mr.ReportError):
            lr.read_rows("/nonexistent/match-report-lastfm-x.csv")


class WrongRate(NoNetwork):
    def rows(self, d_ok, d_wrong, t_ok, t_wrong, t_none=0, no_match=0):
        r = [lr.Row(1, "A", f"d{i}", 200, "duration", "matched", reviewed="ok") for i in range(d_ok)]
        r += [lr.Row(1, "A", f"dw{i}", 200, "duration", "matched", reviewed="wrong") for i in range(d_wrong)]
        r += [lr.Row(1, "A", f"t{i}", None, "title-only", "matched", reviewed="ok") for i in range(t_ok)]
        r += [lr.Row(1, "A", f"tw{i}", None, "title-only", "matched", reviewed="wrong") for i in range(t_wrong)]
        r += [lr.Row(1, "A", f"tn{i}", None, "title-only", "matched") for i in range(t_none)]
        r += [lr.Row(1, "A", f"n{i}", 200, "duration", "no_match") for i in range(no_match)]
        return r

    def test_wrong_rate_is_computed_per_mode(self):
        rows = self.rows(98, 0, 45, 5, no_match=10)  # title-only 5 of 50 = 10%, duration 0%
        text = "\n".join(lr.result_lines(rows))
        self.assertIn("wrong rate title-only: 5 of 50 labelled (10.0%)", text)
        self.assertIn("EXCEEDED", text)
        self.assertIn("wrong rate duration: 0 of 98 labelled (0.0%)", text)
        self.assertIn("wrong rate overall: 5 of 148", text)
        self.assertIn("title-only", lr.verdict(rows))
        self.assertIn("no-go", lr.verdict(rows))

    def test_title_only_judged_on_its_own_even_when_overall_is_fine(self):
        rows = self.rows(990, 0, 8, 2)  # overall 2 of 1000 = 0.2% but title-only 20%
        self.assertLessEqual(lr.wrong_rate(lr.wrong_stats(rows)), 0.02)
        self.assertIn("no-go", lr.verdict(rows))

    def test_go_needs_the_rate_and_labels_in_every_mode(self):
        self.assertTrue(lr.verdict(self.rows(100, 1, 50, 0, no_match=10)).endswith(": go"))
        v = lr.verdict(self.rows(100, 0, 0, 0, t_none=5, no_match=10))
        self.assertTrue(v.startswith("no verdict:"))
        self.assertIn("mode title-only: 0 of 5 needed matched rows labelled", v)
        self.assertIn("no-go", lr.verdict(self.rows(23, 0, 0, 0, no_match=7)))  # 76.7% matched, big enough
        self.assertTrue(lr.verdict([]).startswith("no verdict"))

    def test_boundaries(self):
        self.assertIn("no-go", lr.verdict(self.rows(23, 0, 0, 0, no_match=7)))
        self.assertTrue(lr.verdict(self.rows(24, 0, 0, 0, no_match=6)).endswith(": go"))  # exactly 80%
        self.assertTrue(lr.verdict(self.rows(49, 1, 0, 0)).endswith(": go"))  # exactly 2% wrong
        self.assertIn("within", "\n".join(lr.result_lines(self.rows(49, 1, 0, 0))))
        self.assertIn("no-go", lr.verdict(self.rows(48, 2, 0, 0)))
        self.assertIn("EXCEEDED", "\n".join(lr.result_lines(self.rows(48, 2, 0, 0))))


class NoVerdict(NoNetwork):
    """The gate is never called a go on a sample it cannot judge (reviewer finding on #180)."""

    def ok_rows(self, n, mode="duration", label="ok"):
        d = 200 if mode == "duration" else None
        return [lr.Row(1, "A", f"{mode}{i}", d, mode, "matched", reviewed=label) for i in range(n)]

    def assertNoGo(self, rows, *needles, stop=None):
        v = lr.verdict(rows, stop)
        self.assertTrue(v.startswith("no verdict:"), v)
        self.assertNotRegex(v.replace("no-go", ""), r"\bgo\b")
        for n in needles:
            self.assertIn(n, v)
        self.assertEqual(lr.head_lines(rows, stop)[0], f"verdict: {v}")  # the first line the owner copies
        return v

    def test_99_errors_and_one_ok(self):
        rows = self.ok_rows(1) + [lr.Row(1, "A", f"e{i}", 200, "duration", "error") for i in range(99)]
        self.assertNoGo(rows, "only 1 compared tracks", "99% of the searches failed")

    def test_one_labelled_row(self):
        self.assertNoGo(self.ok_rows(1), "only 1 compared tracks")

    def test_compared_threshold(self):
        self.assertNoGo(self.ok_rows(29), "only 29 compared tracks (need 30)")
        self.assertTrue(lr.verdict(self.ok_rows(30)).endswith(": go"))

    def test_error_share_threshold(self):
        err = lambda n: [lr.Row(1, "A", f"e{i}", 200, "duration", "error") for i in range(n)]
        self.assertTrue(lr.verdict(self.ok_rows(90) + err(10)).endswith(": go"))  # exactly 10%
        self.assertNoGo(self.ok_rows(90) + err(11), "searches failed")

    def test_labels_needed_per_mode_are_min_30_and_matched(self):
        rows = self.ok_rows(40) + self.ok_rows(29, "title-only") + self.ok_rows(11, "title-only", "")
        self.assertNoGo(rows, "mode title-only: 29 of 30 needed")
        self.assertTrue(lr.verdict(self.ok_rows(40) + self.ok_rows(30, "title-only") + self.ok_rows(10, "title-only", "")
                                   ).endswith(": go"))
        self.assertTrue(lr.verdict(self.ok_rows(30) + self.ok_rows(5, "title-only")).endswith(": go"))  # small mode, all labelled
        self.assertNoGo(self.ok_rows(30) + self.ok_rows(5, "title-only", "") , "mode title-only: 0 of 5")

    def test_live_stop_and_stop_from_the_csv(self):
        rows = self.ok_rows(60)
        self.assertNoGo(rows, "the run stopped early (YouTube asks to sign in", stop="YouTube asks to sign in (bot check)")
        text = "\n".join(lr.result_lines(rows, "YouTube asks to sign in"))
        self.assertIn("STOPPED EARLY", text)
        for r in rows:
            r.stopped = "search budget reached"
        self.assertNoGo(rows, "search budget reached")
        path = Path(tempfile.mkdtemp()) / "match-report-lastfm-stop.csv"
        self.addCleanup(lambda: (path.unlink(), path.parent.rmdir()))
        mr.write_csv(path, rows, lr.COLUMNS)
        back = lr.read_rows(str(path))
        self.assertNoGo(back, "search budget reached")
        so = io.StringIO()
        with contextlib.redirect_stdout(so):
            self.assertEqual(lr.main(["--summarize", str(path)]), 0)
        self.assertTrue(so.getvalue().startswith("verdict: no verdict:"))

    def test_nothing_compared(self):
        self.assertTrue(lr.verdict([lr.Row(1, "A", "t", 1, "duration", "stored")]).startswith("no verdict"))

    def test_summary_puts_the_verdict_first(self):
        rows = self.ok_rows(2)
        lines = lr.summary_lines(rows, [], None, None, lr.RunInfo())
        self.assertTrue(lines[0].startswith("verdict: no verdict:"))
        self.assertLess(lines.index("== Last.fm match-rate report =="), len(lines))
        self.assertEqual(sum(1 for line in lines if line.startswith("verdict:")), 1)

    def test_summarize_cleans_terminal_cells(self):
        row = lr.Row(1, "A\x1b[31m", "t\x1b]0;x\x07", 1, "duration", "no_match", "m\x1b[2J", "a\x1b", None, None, 0.1,
                     "r\x1b", "pat\x1b[1mtern", "")
        path = Path(tempfile.mkdtemp()) / "match-report-lastfm-esc.csv"
        self.addCleanup(lambda: (path.unlink(), path.parent.rmdir()))
        mr.write_csv(path, [row], lr.COLUMNS)
        text = path.read_text(encoding="utf-8-sig")
        # a hand-edited file: put raw ESC bytes into every echoed cell
        table = list(csv.reader(text.splitlines()))
        table[1] = [c + "\x1b[31m" if i in (1, 2, 5, 6, 11, 12, 13, 14) else c for i, c in enumerate(table[1])]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(table)
        so = io.StringIO()
        with contextlib.redirect_stdout(so):
            self.assertEqual(lr.main(["--summarize", str(path)]), 0)
        self.assertNotIn("\x1b", so.getvalue())
        self.assertNotIn("\x07", so.getvalue())

    def test_labels_are_normalised_and_unknown_ones_counted(self):
        rows = [lr.Row(1, "A", "t", 1, "duration", "matched", reviewed="ok"),
                lr.Row(1, "A", "u", 1, "duration", "matched", reviewed="maybe"),
                lr.Row(1, "A", "v", 1, "duration", "no_match", reviewed="wrong")]  # label on a non-matched row: ignored
        s = lr.wrong_stats(rows)
        self.assertEqual((s["ok"], s["wrong"], s["other"], s["matched"]), (1, 0, 1, 2))
        self.assertIn("1 with an unrecognised label", "\n".join(lr.result_lines(rows)))

    def test_summary_numbers(self):
        o = lr.ArtistOutcome(1, "Band 1", listed=10, unique=8, unique_no_duration=6)
        o.counts["matched"] = 1
        rows = [lr.Row(1, "Band 1", "a", 200, "duration", "matched"), lr.Row(1, "Band 1", "b", None, "title-only", "no_match")]
        info = lr.RunInfo(ranked=100, last_calls=70, last_budget=500, searches=5, search_budget=600, stored_note="stored check: x")
        text = "\n".join(lr.summary_lines(rows, [o], "time cap reached", Path("match-report-lastfm-t.csv"), info))
        for needle in ("artists ranked: 100  sampled", "tracks listed: 10  unique after collapsing duplicate recordings: 8",
                       "2 with, 6 without (75.0% without); in the sample: 1 with, 1 without", "Last.fm calls: 70 (budget 500)",
                       "YouTube searches: 5 (budget 600)", "stored check: x", "STOPPED EARLY: time cap reached",
                       "--summarize match-report-lastfm-t.csv", "rank 1 Band 1"):
            self.assertIn(needle, text)


class Main(Tmp):
    """main() with Last.fm, YouTube and the key faked: the key must stay out of every stream and file."""

    def go(self, argv, env=None, top=None, tracks=None, ytm=None, ranking_error=None, stored_reader=None):
        out = self.tmp() / "match-report-lastfm-main.csv"
        env = {"LASTFM_API_KEY": KEY} if env is None else env
        client = SimpleNamespace(calls=7)

        def top_artists(*a, **k):
            if ranking_error:
                raise ranking_error
            return top or ranking(4)
        real_ps = mr.PacedSearch
        fake_mod = SimpleNamespace(YTMusic=lambda: ytm or FakeYtm())
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(lr.tg_check, "load_env"), \
                mock.patch.object(lr.lastfm, "LastfmClient", lambda *a, **k: client), \
                mock.patch.object(lr.lastfm, "top_artists", top_artists), \
                mock.patch.object(lastfm_tracks, "artist_tracks",
                                  lambda c, name, mbid, *a, **k: SimpleNamespace(records=(tracks or {}).get(name, []))), \
                mock.patch.dict(sys.modules, {"ytmusicapi": fake_mod}), \
                mock.patch.object(lr.mr, "PacedSearch", lambda y, t, i, m: real_ps(y, t, i, m, sleep=lambda x: None)), \
                mock.patch.object(lr, "read_stored_rows", stored_reader or self.fail), \
                contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            code = lr.main(["--lastfm-user", "some_user", "--out", str(out)] + argv)
        return code, so.getvalue(), se.getvalue(), out

    D1_ENV = {"LASTFM_API_KEY": KEY, "CLOUDFLARE_ACCOUNT_ID": "a", "D1_DATABASE_ID": "b", "INGEST_CLOUDFLARE_API_TOKEN": "c"}

    def test_check_stored_flags_stored_tracks_without_searching_them(self):
        ytm = FakeYtm({"fresh": [result("Fresh", 200, artist="Band 1")]})
        tracks = {"Band 1": [rec("Band 1", "Have It", 200), rec("Band 1", "Fresh", 200)]}
        code, so, se, out = self.go(["--sample-artists", "1", "--max-tracks", "5", "--check-stored"], env=self.D1_ENV,
                                    tracks=tracks, ytm=ytm, stored_reader=lambda: [("Have It", "Band 1", 200)])
        self.assertEqual(code, 0)
        self.assertIn("stored check: read 1 stored rows from D1 (read-only SELECT)", so)
        self.assertIn("stored: 1", so)
        self.assertFalse([q for q in ytm.queries if "have" in q.lower()])
        statuses = {r.title: r.status for r in lr.read_rows(str(out))}
        self.assertEqual(statuses, {"Have It": "stored", "Fresh": "matched"})

    def test_check_stored_skipped_when_d1_is_not_configured_or_fails(self):
        tracks = {"Band 1": [rec("Band 1", "Song", 200)]}
        code, so, se, out = self.go(["--sample-artists", "1", "--check-stored"], tracks=tracks)  # no D1 variables
        self.assertEqual(code, 0)
        self.assertIn("stored check: skipped (needs CLOUDFLARE_ACCOUNT_ID", so)
        self.assertEqual(len(lr.read_rows(str(out))), 1)  # the report went on

        def boom():
            raise IngestError(f"D1 API error 403 {KEY}")
        code, so, se, out = self.go(["--sample-artists", "1", "--check-stored"], env=self.D1_ENV, tracks=tracks,
                                    stored_reader=boom)
        self.assertEqual(code, 0)
        self.assertIn("stored check: skipped (D1 read failed: IngestError", so)
        self.assertNotIn(KEY, so + se)

    def test_stored_check_is_off_unless_asked(self):
        code, so, *_ = self.go(["--sample-artists", "1"], env=self.D1_ENV, tracks={"Band 1": [rec("Band 1", "S", 200)]})
        self.assertEqual(code, 0)  # stored_reader=self.fail would raise if it were read
        self.assertIn("not requested", so)

    def test_sign_in_stop_is_in_the_csv_and_the_first_line_is_the_verdict(self):
        tracks = {"Band 1": [rec("Band 1", f"Song {i}", 200) for i in range(5)]}
        code, so, se, out = self.go(["--sample-artists", "1"], tracks=tracks,
                                    ytm=FakeYtm(error=YouTubeSignInRequired("Sign in to confirm")))
        self.assertEqual(code, 2)
        self.assertTrue(so.startswith("verdict: no verdict:"))
        self.assertIn("STOPPED EARLY", so)
        self.assertTrue(all("not bypassed" in r.stopped for r in lr.read_rows(str(out))))

    def test_happy_path_prints_a_summary_and_writes_the_csv(self):
        ytm = FakeYtm({"song": [result("Song", 200, artist="Band 1")]})
        tracks = {"Band 1": [rec("Band 1", "Song", 200)], "Band 3": [rec("Band 3", "Other", None)]}
        code, so, se, out = self.go(["--sample-artists", "2", "--max-tracks", "5"], tracks=tracks, ytm=ytm)
        self.assertEqual(code, 0)
        self.assertIn("== Last.fm match-rate report ==", so)
        self.assertIn("mode title-only", so)
        for stream in (so, se, out.read_text(encoding="utf-8-sig")):
            self.assertNotIn(KEY, stream)
            self.assertNotIn("some_user", stream)
        self.assertEqual(len(list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))), 3)

    def test_missing_or_malformed_key_is_a_clean_error(self):
        for env in ({}, {"LASTFM_API_KEY": "short"}):
            code, so, se, out = self.go([], env=env)
            self.assertEqual(code, 1)
            self.assertIn("ERROR", se)
            self.assertNotIn("Traceback", se)
            self.assertFalse(out.exists())

    def test_ranking_failure_redacts_the_key_and_writes_nothing(self):
        code, so, se, out = self.go([], ranking_error=lastfm.LastfmError(f"oops {KEY}"))
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, se)
        self.assertFalse(out.exists())

    def test_bad_arguments_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["--max-tracks", "0"], ["--max-minutes", "0"], ["--max-minutes", "9999"], ["--min-interval", "0.2"],
                         ["--sample-artists", "51"], ["--years", "11"]):
                with self.assertRaises(SystemExit):
                    self.go(argv)
        code, *_ = self.go(["--sample-artists", "5", "--max-tracks", "3"])
        self.assertEqual(code, 1)

    def test_summarize_is_offline_and_needs_no_key(self):
        d = self.tmp()
        path = d / "match-report-lastfm-s.csv"
        mr.write_csv(path, [lr.Row(1, "A", "t", None, "title-only", "matched", "t", "A", 200.0, None, 0.99, reviewed="wrong")],
                     lr.COLUMNS)
        so = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(so):
            self.assertEqual(lr.main(["--summarize", str(path)]), 0)
        self.assertIn("wrong rate title-only: 1 of 1 labelled (100.0%)", so.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lr.main(["--summarize", str(d / "missing.csv")]), 1)


if __name__ == "__main__":
    unittest.main()

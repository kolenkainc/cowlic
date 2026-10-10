"""Tests for match_report: fake catalog and fake YouTube Music search, no network, no sleeping."""
import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import match_report as mr
from ingest import Throttle

A1, A2, A3 = "A" * 22, "B" * 22, "C" * 22


def song(n, name=None, dur=200.0, artist="Art", **kw):
    r = {"song_id": f"T{n}".ljust(22, "x"), "name": name if name is not None else f"Song {n}", "artists": [artist],
         "album_artist": artist, "album_name": "Alb", "year": 2020, "duration": dur, "album_type": "album",
         "release_date": "2020-01-01", "explicit": False}
    r.update(kw)
    return r


class FakeYtm:
    """search(query, filter, limit) -> results; `table` maps a lower-cased title to a list of results."""

    def __init__(self, table=None, error=None):
        self.table, self.error, self.queries = table or {}, error, []

    def search(self, query, **kw):
        self.queries.append(query)
        if self.error:
            raise self.error
        for key, res in self.table.items():
            if key in query.lower():
                return res
        return []


def result(title, dur, vid="vid00000001", artist="Art"):
    return {"videoId": vid, "title": title, "duration_seconds": dur, "artists": [{"name": artist}]}


def make_ps(ytm, max_searches=1000, interval=1.0, sleeps=None, clock=None):
    sleeps = sleeps if sleeps is not None else []
    t = [0.0]
    return mr.PacedSearch(ytm, Throttle(0, 0, 1, sleep=sleeps.append, clock=lambda: t[0]), interval, max_searches,
                          sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)), clock=clock or (lambda: t[0]))


def quiet(*a):
    pass


def do_run(catalog, ytm, ids=None, max_tracks=150, secrets=(), **kw):
    rows = []
    ps = make_ps(ytm, **kw)
    ids = ids or list(catalog)
    outcomes, stop = mr.run(ids, lambda a: catalog[a] if not isinstance(catalog[a], Exception) else (_ for _ in ()).throw(catalog[a]),
                            ps, max_tracks, rows, list(secrets), out=quiet)
    return rows, outcomes, stop, ps


class Counts(unittest.TestCase):
    def test_statuses_rate_and_closest(self):
        ytm = FakeYtm({"song 1": [result("Song 1", 201)],                       # match
                       "song 2": [result("Song 2", 230)],                       # right title, wrong duration
                       "song 3": [result("Totally Different", 200)]})           # wrong title
        cat = {A1: [song(1), song(2), song(3), song(4, dur=0), song(5)]}       # 4 unusable duration, 5 no results
        rows, outcomes, stop, _ = do_run(cat, ytm)
        by = {r.title: r for r in rows}
        self.assertIsNone(stop)
        self.assertEqual(by["Song 1"].status, "matched")
        self.assertAlmostEqual(by["Song 1"].delta, 1.0)
        self.assertEqual((by["Song 2"].status, by["Song 2"].reason), ("no_match", "duration"))
        self.assertAlmostEqual(by["Song 2"].delta, 30.0)
        self.assertEqual((by["Song 3"].status, by["Song 3"].reason), ("no_match", "title"))
        self.assertEqual(by["Song 4"].status, "uncomparable")
        self.assertEqual((by["Song 5"].status, by["Song 5"].reason), ("no_match", "no_results"))
        c = outcomes[0].counts
        self.assertEqual((c["matched"], c["no_match"], c["uncomparable"], c["error"]), (1, 3, 1, 0))
        self.assertAlmostEqual(mr.rate(c), 0.25)

    def test_rate_ignores_uncomparable_and_error(self):
        self.assertAlmostEqual(mr.rate({"matched": 3, "no_match": 1, "uncomparable": 9, "error": 9}), 0.75)
        self.assertIsNone(mr.rate({"matched": 0, "no_match": 0, "uncomparable": 2, "error": 0}))

    def test_verdict_is_the_92_gate(self):
        go = "matched rate >= 80%: go IF the owner's spot-check of matched rows finds <= 2% wrong"
        no = "matched rate < 80%: no-go for now (tune the matcher; see the top failing patterns)"
        self.assertEqual(mr.verdict(0.80), go)
        self.assertEqual(mr.verdict(0.82), go)
        self.assertEqual(mr.verdict(0.7999), no)
        self.assertEqual(mr.verdict(0.0), no)
        self.assertTrue(mr.verdict(None).startswith("no verdict"))

    def test_small_sample_and_error_warnings(self):
        rows = [mr.Row("A", A1, f"t{i}", 1.0, "matched") for i in range(10)]
        text = "\n".join(mr.summary_lines(rows, [], None, None))
        self.assertIn("only a hint", text)
        self.assertNotIn("WARNING", text)
        rows += [mr.Row("A", A1, "e", 1.0, "error") for _ in range(2)]  # 2 of 12 = 16.7%
        text = "\n".join(mr.summary_lines(rows, [], None, None))
        self.assertIn("WARNING: verdict may be misleading: 17% of searches failed", text)
        self.assertIn("search errors: 2 of 12", text)
        ok = rows[:10] + [mr.Row("A", A1, "e", 1.0, "error")] * 1  # 1 of 11 = 9.1%
        self.assertNotIn("WARNING", "\n".join(mr.summary_lines(ok, [], None, None)))

    def test_spot_check(self):
        self.assertIsNone(mr.parse_spot_check(None, None))
        self.assertEqual(mr.parse_spot_check(1, 50), (1, 50))
        for bad in ((1, None), (None, 5), (6, 5), (-1, 5), (0, 0)):
            with self.assertRaises(mr.ReportError, msg=bad):
                mr.parse_spot_check(*bad)
        rows = [mr.Row("A", A1, "t", 1.0, "matched")]
        self.assertIn("within", "\n".join(mr.summary_lines(rows, [], None, None, (1, 50))))
        self.assertIn("EXCEEDED", "\n".join(mr.summary_lines(rows, [], None, None, (2, 50))))

    def test_duplicates_are_collapsed_before_the_search(self):
        ytm = FakeYtm({"song 1": [result("Song 1", 200)]})
        cat = {A1: [song(1), song(2, name="Song 1 (Remastered 2011)", dur=200.5)]}
        rows, outcomes, _, _ = do_run(cat, ytm)
        self.assertEqual((outcomes[0].listed, outcomes[0].unique, len(rows)), (2, 1, 1))


class Caps(unittest.TestCase):
    def test_max_tracks_is_a_hard_cap_shared_fairly(self):
        cat = {A1: [song(i) for i in range(1, 30)], A2: [song(i, artist="Two") for i in range(100, 130)]}
        rows, outcomes, _, ps = do_run(cat, FakeYtm(), max_tracks=10)
        self.assertEqual(len(rows), 10)
        self.assertEqual([o.sampled for o in outcomes], [5, 5])
        self.assertLessEqual(ps.searches, 3 * 10)

    def test_unused_budget_flows_to_later_artists(self):
        cat = {A1: [song(1)], A2: [song(i, artist="Two") for i in range(100, 130)]}
        rows, outcomes, _, _ = do_run(cat, FakeYtm(), max_tracks=10)
        self.assertEqual([o.sampled for o in outcomes], [1, 9])

    def test_max_tracks_below_artist_count_is_refused(self):
        with self.assertRaises(mr.ReportError):
            do_run({A1: [song(1)], A2: [song(2)]}, FakeYtm(), max_tracks=1)

    def test_search_budget_stops_the_run(self):
        cat = {A1: [song(i) for i in range(1, 20)]}
        rows, outcomes, stop, ps = do_run(cat, FakeYtm(), max_tracks=19, max_searches=5)
        self.assertEqual(stop, "search budget reached")
        self.assertEqual(ps.searches, 5)

    def test_sample_is_deterministic_and_order_independent(self):
        songs = [song(i) for i in range(1, 40)]
        a = [s["song_id"] for s in mr.sample(songs, 10)]
        b = [s["song_id"] for s in mr.sample(list(reversed(songs)), 10)]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 10)
        titles = [s["name"].casefold() for s in mr.sample(songs, 10)]
        self.assertEqual(titles, sorted(titles))

    def test_searches_are_paced(self):
        sleeps = []
        cat = {A1: [song(i) for i in range(1, 4)]}
        _, _, _, ps = do_run(cat, FakeYtm(), sleeps=sleeps, interval=1.0)
        self.assertEqual(ps.searches, 9)  # 3 queries per track (no hit): title+album, artist title, transliteration
        paced = [s for s in sleeps if s > 0]
        self.assertEqual(len(paced), 8)  # none before the first search
        self.assertTrue(all(s >= 0.999 for s in paced))


class Errors(unittest.TestCase):
    def test_one_artist_failing_does_not_abort_the_others(self):
        secret = "sekrit-token-123456"
        cat = {A1: RuntimeError(f"boom {secret}"), A2: [song(1)]}
        rows, outcomes, stop, _ = do_run(cat, FakeYtm({"song 1": [result("Song 1", 200)]}), secrets=[secret])
        self.assertIsNone(stop)
        self.assertIn("RuntimeError", outcomes[0].error)
        self.assertNotIn(secret, outcomes[0].error)
        self.assertEqual([r.status for r in rows], ["matched"])
        lines = "\n".join(mr.summary_lines(rows, outcomes, stop, None))
        self.assertIn("FAILED", lines)
        self.assertNotIn(secret, lines)

    def test_search_error_becomes_a_row_and_three_in_a_row_stop(self):
        cat = {A1: [song(i) for i in range(1, 10)]}
        rows, outcomes, stop, _ = do_run(cat, FakeYtm(error=ValueError("nope")))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.status == "error" for r in rows))
        self.assertIn("3 searches failed", stop)

    def test_sign_in_error_stops_immediately(self):
        cat = {A1: [song(i) for i in range(1, 10)]}
        err = RuntimeError("Sign in to confirm you're not a bot")
        rows, _, stop, _ = do_run(cat, FakeYtm(error=err))
        self.assertEqual(len(rows), 1)
        self.assertIn("sign in", stop)

    def test_uncomparable_rows_do_not_reset_the_error_streak(self):
        songs = [song(1), song(2, dur=0), song(3), song(4, dur=0), song(5), song(6)]
        rows, _, stop, _ = do_run({A1: songs}, FakeYtm(error=ValueError("x")))
        self.assertIsNotNone(stop)
        self.assertEqual(sum(r.status == "error" for r in rows), 3)

    def test_success_resets_the_error_streak(self):
        calls = {"n": 0}

        def flaky(ps, name, artist, album, dur):
            calls["n"] += 1
            if calls["n"] % 3 != 0:
                raise ValueError("x")
            return None

        rows = []
        mr.run([A1], lambda a: [song(i) for i in range(1, 10)], make_ps(FakeYtm()), 150, rows, [], out=quiet, find=flaky)
        self.assertEqual(len(rows), 9)

    def test_error_message_is_redacted_in_the_log(self):
        logged = []
        rows = []
        mr.run([A1], lambda a: [song(1)], make_ps(FakeYtm(error=ValueError("token=SECRETVALUE"))), 5, rows,
               ["SECRETVALUE"], out=logged.append)
        self.assertNotIn("SECRETVALUE", "\n".join(logged))


class Csv(unittest.TestCase):
    def write(self, rows):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "match-report-x.csv"
            mr.write_csv(p, rows)
            raw = p.read_bytes()
            mode = p.stat().st_mode & 0o777
        text = raw.decode("utf-8-sig")
        return raw, mode, list(csv.reader(io.StringIO(text, newline="")))

    def row(self, title, artist="Art", **kw):
        return mr.Row(artist, A1, title, 200.0, "no_match", **kw)

    def test_formula_injection_is_neutralised(self):
        for bad in ("=1+1", "+1", "-1", "@SUM(A1)", "=cmd|' /C calc'!A0", "\t=1+1", "\r=1+1", " =1+1"):
            _, _, table = self.write([self.row(bad, artist=bad, matched_title=bad, reason=bad)])
            for cell in (table[1][0], table[1][2], table[1][4], table[1][9]):
                self.assertFalse(cell.lstrip().startswith(("=", "+", "-", "@")), (bad, cell))
                self.assertTrue(cell.startswith("'"), (bad, cell))

    def test_numbers_are_not_prefixed_and_negative_delta_stays_numeric(self):
        r = mr.Row("Art", A1, "S", 200.0, "matched", "S", 197.0, -3.0, 1.0)
        _, _, table = self.write([r])
        self.assertEqual(table[1][3:8], ["200.0", "S", "197.0", "-3.0", "1.000"])

    def test_commas_quotes_newlines_unicode_and_controls(self):
        title = 'He said, "hi"\nline2\x1b[31m\x00 Привет 日本語 ✓'
        raw, mode, table = self.write([self.row(title), self.row("plain")])
        self.assertEqual(len(table), 3)  # header + 2 rows: the newline did not split a record
        self.assertEqual(table[0], mr.COLUMNS)
        self.assertEqual(table[1][2], 'He said, "hi" line2[31m Привет 日本語 ✓')
        self.assertNotIn(b"\x1b", raw)
        self.assertNotIn(b"\x00", raw)
        self.assertEqual(mode, 0o600)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))

    def test_bidi_and_zero_width_are_stripped(self):
        self.assertEqual(mr.clean_text("a‮b​c d"), "abc d")

    def test_long_text_is_bounded_and_non_strings_are_empty(self):
        self.assertEqual(len(mr.clean_text("x" * 10000)), mr.TEXT_MAX)
        self.assertEqual(mr.clean_text(None), "")

    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "match-report-1.csv"
            p.write_text("keep")
            with self.assertRaises(mr.ReportError):
                mr.validate_out(str(p))
            with self.assertRaises(FileExistsError):
                mr.write_csv(p, [])
            self.assertEqual(p.read_text(), "keep")

    def test_output_name_must_match_the_gitignore_pattern(self):
        for bad in ("report.csv", "match-report.txt", "x/match-report-1.csv/..", "my-match-report.csv"):
            with self.assertRaises(mr.ReportError, msg=bad):
                mr.validate_out(bad)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mr.validate_out(str(Path(d) / "match-report-ok.csv")).name, "match-report-ok.csv")

    def test_gitignore_has_the_pattern(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn("match-report*.csv", (root / ".gitignore").read_text().splitlines())


class Patterns(unittest.TestCase):
    def test_rules_and_priority(self):
        cases = [
            ("Heroes (2017 Remaster)", "title", "remaster"),
            ("Album Name - Deluxe Edition Bonus Track", "title", "remaster"),
            ("25th Anniversary Mix", "title", "remaster"),  # remaster outranks remix
            ("Wish You Were Here - Live at Wembley", "duration", "live"),
            ("Something Acoustic Version", "title", "live"),
            ("Radio Session", "title", "live"),
            ("Some Long Song Title (Club Remix)", "title", "remix"),
            ("Some Long Song Title - Radio Edit", "title", "remix"),
            ("Драма", "title", "non-latin"),
            ("Очень длинное название песни", "duration", "non-latin"),
            ("夜に駆ける", "title", "non-latin"),
            ("Café Tacvba Song Of Love", "title", "other"),  # accented Latin is Latin
            ("A Longer Song Name (feat. Someone)", "title", "featuring"),
            ("A Longer Song Name ft. Someone", "title", "featuring"),
            ("Intro", "no_results", "short-generic"),
            ("Untitled", "title", "short-generic"),
            ("Hotel California", "duration", "short-generic"),  # <= 2 words
            ("Go", "title", "short-generic"),
            ("Walking On Broken Glass", "duration", "duration-only"),
            ("Walking On Broken Glass", "no_results", "no-results"),
            ("Walking On Broken Glass", "title", "other"),
            ("", "title", "other"),
        ]
        for title, reason, want in cases:
            self.assertEqual(mr.failure_pattern(title, reason), want, (title, reason))
        self.assertTrue(set(c[2] for c in cases) <= set(mr.PATTERNS))

    def test_rows_are_tagged_and_counted_most_frequent_first(self):
        ytm = FakeYtm({"драма": [result("Другая песня", 200)], "remaster": [result("Remaster Title X", 250)]})
        cat = {A1: [song(1, name="Драма"), song(2, name="Драма Два"),
                    song(3, name="Walking On Broken Glass (Remastered)"),
                    song(4, name="Walking On Broken Glass", dur=100.0),
                    song(5, name="Matched Song Title Here")]}
        ytm.table["matched song"] = [result("Matched Song Title Here", 200)]
        rows, outcomes, _, _ = do_run(cat, ytm)
        by = {r.title: r for r in rows}
        self.assertEqual(by["Драма"].pattern, "non-latin")
        self.assertEqual(by["Walking On Broken Glass (Remastered)"].pattern, "remaster")
        self.assertEqual(by["Walking On Broken Glass"].pattern, "no-results")
        self.assertEqual(by["Matched Song Title Here"].pattern, "")  # only failures are tagged
        counts = mr.pattern_counts(rows)
        self.assertEqual(counts[0], ("non-latin", 2))
        self.assertEqual(sum(n for _, n in counts), sum(1 for r in rows if r.status == "no_match"))
        text = "\n".join(mr.summary_lines(rows, outcomes, None, None))
        self.assertIn("top failing patterns", text)
        self.assertLess(text.index("non-latin"), text.index("remaster\n") if "remaster\n" in text else 10 ** 9)

    def test_pattern_column_in_csv(self):
        self.assertIn("pattern", mr.COLUMNS)
        self.assertIn("reviewed", mr.COLUMNS)
        r = mr.Row("A", A1, "Драма", 1.0, "no_match", pattern="non-latin")
        cells = r.cells()
        self.assertEqual(len(cells), len(mr.COLUMNS))
        self.assertEqual(cells[mr.COLUMNS.index("pattern")], "non-latin")
        self.assertEqual(cells[mr.COLUMNS.index("reviewed")], "")


class Ordering(unittest.TestCase):
    def test_output_is_identical_across_runs_and_follows_artist_order(self):
        cat = {A2: [song(i, artist="Two") for i in range(1, 8)], A1: [song(i + 50) for i in range(1, 8)]}
        ytm = lambda: FakeYtm({"song 1": [result("Song 1", 200)]})
        r1, *_ = do_run(cat, ytm(), ids=[A2, A1], max_tracks=10)
        r2, *_ = do_run(cat, ytm(), ids=[A2, A1], max_tracks=10)
        self.assertEqual([r.cells() for r in r1], [r.cells() for r in r2])
        self.assertEqual({r.artist_id for r in r1[:5]}, {A2})
        self.assertEqual({r.artist_id for r in r1[5:]}, {A1})

    def test_worst_rows_lowest_similarity_first(self):
        rows = [mr.Row("A", A1, "t1", 1.0, "no_match", similarity=0.7),
                mr.Row("A", A1, "t2", 1.0, "no_match", similarity=0.2),
                mr.Row("A", A1, "t3", 1.0, "no_match", similarity=None),
                mr.Row("A", A1, "t4", 1.0, "matched", similarity=0.1)]
        self.assertEqual([r.title for r in mr.worst_rows(rows)], ["t3", "t2", "t1"])
        self.assertEqual(len(mr.worst_rows(rows * 20)), mr.WORST_ROWS)


class Summary(unittest.TestCase):
    def test_per_artist_and_overall(self):
        ytm = FakeYtm({"song 1": [result("Song 1", 200)], "song 2": [result("Song 2", 200)]})
        cat = {A1: [song(1), song(2)], A2: [song(3, artist="Two"), song(4, artist="Two")]}
        rows, outcomes, stop, _ = do_run(cat, ytm)
        text = "\n".join(mr.summary_lines(rows, outcomes, stop, Path("match-report-z.csv")))
        self.assertIn("matched: 2  no_match: 2", text)
        self.assertIn("50.0%", text)
        self.assertIn("no-go for now", text)
        self.assertIn(f"Art ({A1})", text)
        self.assertIn(f"Two ({A2})", text)
        self.assertIn("rate 100.0%", text)
        self.assertIn("rate 0.0%", text)
        self.assertIn("lowest-similarity failures", text)
        self.assertIn("CSV: match-report-z.csv", text)

    def test_terminal_text_has_no_control_characters(self):
        rows = [mr.Row("A\x1b[2J", A1, "t\x07\nx", 1.0, "no_match", matched_title="\x1b]0;x\x07m", similarity=0.1)]
        text = "\n".join(mr.summary_lines(rows, [], None, None))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_stop_is_shown(self):
        self.assertIn("STOPPED EARLY", "\n".join(mr.summary_lines([], [], "why", None)))


class Inputs(unittest.TestCase):
    def test_ids_urls_and_dedupe(self):
        url = f"https://open.spotify.com/artist/{A2}?si=abc"
        self.assertEqual(mr.collect_artists([A1, url, A1], None), [A1, A2])

    def test_invalid_is_rejected_not_rewritten(self):
        for bad in ("short", f"https://evil.example/artist/{A1}", f"http://open.spotify.com/artist/{A1}", A1 + "x", ""):
            with self.assertRaises(Exception, msg=bad):
                mr.collect_artists([bad], None)

    def test_none_and_too_many(self):
        with self.assertRaises(mr.ReportError):
            mr.collect_artists([], None)
        many = [("%022d" % i) for i in range(mr.MAX_ARTISTS + 1)]
        with self.assertRaises(mr.ReportError):
            mr.collect_artists(many, None)

    def test_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text(f"# the sample\n\n{A1}\n  https://open.spotify.com/artist/{A2}  \n")
            self.assertEqual(mr.collect_artists([], str(p)), [A1, A2])
            p.write_text(f"{A1}\nnot an artist\n")
            with self.assertRaises(mr.ReportError) as cm:
                mr.collect_artists([], str(p))
            self.assertIn("line 2", str(cm.exception))
            p.write_bytes(b"\xff\xfe\x00")
            with self.assertRaises(mr.ReportError):
                mr.collect_artists([], str(p))
            with self.assertRaises(mr.ReportError):
                mr.collect_artists([], str(Path(d) / "missing.txt"))
            p.write_text("x" * (mr.ARTISTS_FILE_MAX_BYTES + 1))
            with self.assertRaises(mr.ReportError):
                mr.collect_artists([], str(p))

    def test_cli_limits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["--min-interval", "0.5"], ["--min-interval", "nan"], ["--max-tracks", "0"],
                         ["--max-tracks", "100000"]):
                with self.assertRaises(SystemExit, msg=argv):
                    mr.main([A1, *argv])


class Main(unittest.TestCase):
    def test_end_to_end_with_fakes_writes_csv_and_exit_code(self):
        class FakeYTMusic:
            def search(self, q, **kw):
                return [result("Song 1", 200)]

        env = {"SPOTIFY_CLIENT_ID": "cid-canary", "SPOTIFY_CLIENT_SECRET": "secret-canary"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
                mock.patch.object(mr.tg_check, "load_env"), \
                mock.patch.object(mr, "artist_tracks", lambda cat, a, out=None: [song(1)]), \
                mock.patch.dict("sys.modules", {"ytmusicapi": mock.Mock(YTMusic=FakeYTMusic)}):
            out = io.StringIO()
            path = str(Path(d) / "match-report-t.csv")
            with contextlib.redirect_stdout(out):
                code = mr.main([A1, "--out", path])
            self.assertEqual(code, 0)
            self.assertIn("100.0%", out.getvalue())
            self.assertNotIn("secret-canary", out.getvalue())
            self.assertEqual(len(Path(path).read_text(encoding="utf-8-sig").splitlines()), 2)

    def test_exit_code_2_when_an_artist_failed_but_csv_and_summary_are_written(self):
        def tracks(cat, a, out=None):
            if a == A1:
                raise mr.ArtistTracksError("catalog down")
            return [song(1)]

        class FakeYTMusic:
            def search(self, q, **kw):
                return [result("Song 1", 200)]

        env = {"SPOTIFY_CLIENT_ID": "cid-canary", "SPOTIFY_CLIENT_SECRET": "secret-canary"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
                mock.patch.object(mr.tg_check, "load_env"), mock.patch.object(mr, "artist_tracks", tracks), \
                mock.patch.dict("sys.modules", {"ytmusicapi": mock.Mock(YTMusic=FakeYTMusic)}):
            out = io.StringIO()
            path = Path(d) / "match-report-t.csv"
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = mr.main([A1, A2, "--out", str(path)])
            self.assertEqual(code, 2)
            self.assertIn("FAILED", out.getvalue())
            self.assertTrue(path.exists())

    def test_missing_credentials_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(mr.tg_check, "load_env"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(mr.main([A1, "--out", "match-report-never.csv"]), 1)
        self.assertIn("SPOTIFY_CLIENT_ID", err.getvalue())
        self.assertFalse(Path("match-report-never.csv").exists())


if __name__ == "__main__":
    unittest.main()

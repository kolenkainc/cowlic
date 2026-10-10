"""Tests for dedupe: pure function, real-shaped records, no I/O."""
import contextlib
import copy
import io
import multiprocessing
import random
import time
import unittest

import dedupe
from dedupe import collapse, title_key


def rec(sid, name="Song", artist="Art", dur=200.0, rtype="album", date="2019-05-03", explicit=False, **kw):
    r = {"song_id": sid, "name": name, "artists": [artist], "album_artist": artist, "album_name": "X",
         "year": int(date[:4]) if date else None, "track_number": 1, "disc_number": 1, "duration": dur,
         "url": f"https://open.spotify.com/track/{sid}", "cover_url": None,
         "album_type": rtype, "release_date": date, "explicit": explicit}
    r.update(kw)
    return r


def ids(result):
    return [r["song_id"] for r in result.kept]


class TitleKey(unittest.TestCase):
    def test_table(self):
        cases = [
            ("Song", ("song", False, False)),
            ("Song (Remastered 2011)", ("song", False, False)),
            ("Song - 2011 Remaster", ("song", False, False)),
            ("Song - Remastered", ("song", False, False)),
            ("Song [Deluxe Edition]", ("song", False, False)),
            ("Song (40th Anniversary Edition)", ("song", False, False)),
            ("Song (Bonus Track)", ("song", False, False)),
            ("Song (Explicit)", ("song", False, True)),
            ("Song (Clean Version)", ("song", False, False)),
            ("Song - Live at Wembley", ("song", True, False)),
            ("Song (Live)", ("song", True, False)),
            ("Song (Live) (Remastered 2009)", ("song", True, False)),
            # not tags: stay in the key
            ("Song (Remix)", ("song remix", False, False)),
            ("Song - Radio Edit", ("song radio edit", False, False)),
            ("Song (Acoustic)", ("song acoustic", False, False)),
            ("Song (feat. Guest)", ("song feat guest", False, False)),
            ("Song - Live Wire", ("song live wire", False, False)),
            ("Song (Live Remix)", ("song live remix", False, False)),
            ("Song (Deluxe Remix)", ("song deluxe remix", False, False)),
            ("Alive", ("alive", False, False)),
            ("Песня (Remastered 2011)", ("песня", False, False)),
            ("ＳＯＮＧ", ("song", False, False)),  # NFKC
            ("(Deluxe)", ("", False, False)),
        ]
        for title, want in cases:
            with self.subTest(title=title):
                self.assertEqual(title_key(title), want)


class Rules(unittest.TestCase):
    def test_same_song_single_album_deluxe_compilation(self):
        r = [rec("c", "Song", rtype="compilation", date="2021-01-01"),
             rec("d", "Song - Deluxe Edition", rtype="album", date="2020-01-01"),
             rec("s", "Song", rtype="single", date="2019-03-01", dur=201.2),
             rec("a", "Song", rtype="album", date="2019-05-03", dur=199.0)]
        res = collapse(r)
        self.assertEqual(ids(res), ["s"])  # earliest release wins
        self.assertEqual((res.listed, res.unique, res.dropped), (4, 1, 3))

    def test_tie_album_over_single_over_compilation(self):
        r = [rec("c", rtype="compilation"), rec("s", rtype="single"), rec("a", rtype="album")]
        self.assertEqual(ids(collapse(r)), ["a"])
        self.assertEqual(ids(collapse([r[0], r[1]])), ["s"])
        self.assertEqual(ids(collapse(list(reversed(r)))), ["a"])

    def test_tie_on_everything_keeps_first_in_input_order(self):
        r = [rec("x1"), rec("x2")]
        self.assertEqual(ids(collapse(r)), ["x1"])
        self.assertEqual(ids(collapse(r[::-1])), ["x2"])

    def test_earliest_beats_album_type(self):
        r = [rec("a", rtype="album", date="2020-01-01"), rec("s", rtype="single", date="2019-01-01")]
        self.assertEqual(ids(collapse(r)), ["s"])

    def test_reduced_precision_date_sorts_after_full_dates_of_that_year(self):
        r = [rec("y", date="2019", rtype="album"), rec("f", date="2019-12-31", rtype="single")]
        self.assertEqual(ids(collapse(r)), ["f"])
        r = [rec("n", date=None, year=None, rtype="album"), rec("y", date="2019", rtype="compilation")]
        self.assertEqual(ids(collapse(r)), ["y"])

    def test_year_field_is_the_fallback_for_a_missing_release_date(self):
        r = [rec("n", date=None, year=2021), rec("o", date=None, year=2018, rtype="compilation")]
        self.assertEqual(ids(collapse(r)), ["o"])

    def test_remaster_collapses(self):
        r = [rec("o", "Song", date="1999-01-01"), rec("r", "Song - 2011 Remaster", date="2011-01-01")]
        res = collapse(r)
        self.assertEqual(ids(res), ["o"])
        self.assertEqual(res.kept[0]["name"], "Song")  # displayed title untouched

    def test_displayed_title_is_untouched(self):
        r = [rec("r", "Song (Remastered 2011)", date="2011-01-01"), rec("o", "Song", date="2012-01-01")]
        self.assertEqual(collapse(r).kept[0]["name"], "Song (Remastered 2011)")

    def test_live_stays_separate_by_default(self):
        r = [rec("st", "Song"), rec("lv", "Song - Live at Wembley", date="2020-01-01")]
        self.assertEqual(ids(collapse(r)), ["st", "lv"])

    def test_explicit_and_clean_stay_separate_by_default(self):
        r = [rec("ex", "Song", explicit=True), rec("cl", "Song", explicit=False, date="2020-01-01")]
        self.assertEqual(ids(collapse(r)), ["ex", "cl"])
        r = [rec("ex", "Song (Explicit)", explicit=None), rec("cl", "Song (Clean Version)", explicit=None)]
        self.assertEqual(ids(collapse(r)), ["ex", "cl"])

    def test_two_explicit_releases_still_collapse(self):
        r = [rec("a", explicit=True), rec("b", explicit=True, rtype="single", date="2020-01-01")]
        self.assertEqual(ids(collapse(r)), ["a"])

    def test_flag_merges_variants(self):
        r = [rec("lv", "Song - Live at Wembley", date="2018-01-01"), rec("st", "Song"),
             rec("ex", "Song", explicit=True, date="2020-01-01"), rec("cl", "Song (Clean)", date="2021-01-01")]
        res = collapse(r, merge_variants=True)
        self.assertEqual(ids(res), ["lv"])
        self.assertEqual(len(collapse(r).kept), 3)  # default: studio+clean, explicit, live

    def test_remix_and_version_titles_are_never_merged_with_the_original(self):
        for t in ("Song (Remix)", "Song (Acoustic)", "Song - Radio Edit", "Song (Instrumental)", "Song (Live Remix)"):
            with self.subTest(t=t):
                r = [rec("o", "Song"), rec("v", t, date="2020-01-01")]
                self.assertEqual(ids(collapse(r)), ["o", "v"])
                self.assertEqual(ids(collapse(r, merge_variants=True)), ["o", "v"])

    def test_different_artist_or_title_is_not_merged(self):
        r = [rec("a", artist="Art"), rec("b", artist="Other"), rec("c", name="Other Song")]
        self.assertEqual(ids(collapse(r)), ["a", "b", "c"])

    def test_primary_artist_is_the_track_artist_not_the_compilation_album_artist(self):
        r = [rec("a", "Song"), rec("c", "Song", rtype="compilation", date="2020-01-01", album_artist="Various Artists")]
        self.assertEqual(ids(collapse(r)), ["a"])

    def test_album_artist_is_the_fallback(self):
        r = [rec("a", "Song", artists=[]), rec("b", "Song", artists=[], date="2020-01-01")]
        self.assertEqual(ids(collapse(r)), ["a"])
        r = [rec("a", "Song", artists=[], album_artist=None), rec("b", "Song", artists=[], album_artist=None)]
        self.assertEqual(ids(collapse(r)), ["a", "b"])

    def test_unicode_and_case(self):
        r = [rec("a", "Пе́сня", artist="Артист"), rec("b", "ПЕ́СНЯ (Remastered)", artist="артист", date="2020-01-01")]
        self.assertEqual(ids(collapse(r)), ["a"])
        r = [rec("a", "Café"), rec("b", "CAFÉ", date="2020-01-01")]  # NFKC: composed vs combining accent
        self.assertEqual(ids(collapse(r)), ["a"])

    def test_old_records_without_new_fields(self):
        r = [{"song_id": "a", "name": "Song", "artists": ["Art"], "year": 2019, "duration": 200.0},
             {"song_id": "b", "name": "Song", "artists": ["Art"], "year": 2020, "duration": 201.0}]
        self.assertEqual(ids(collapse(r)), ["a"])


class Duration(unittest.TestCase):
    def test_tolerance_boundary(self):
        for other, merged in ((203.0, True), (197.0, True), (202.9, True), (203.01, False), (196.9, False)):
            with self.subTest(other=other):
                r = [rec("a", dur=200.0), rec("b", dur=other, date="2020-01-01")]
                self.assertEqual(len(collapse(r).kept), 1 if merged else 2)

    def test_float_noise_at_the_boundary(self):
        r = [rec("a", dur=200.3), rec("b", dur=203.3, date="2020-01-01")]  # 203.3 - 200.3 = 3.0000000000000284
        self.assertEqual(len(collapse(r).kept), 1)

    def test_no_chaining(self):
        r = [rec("a", dur=200.0, date="2019-01-01"), rec("b", dur=202.5, date="2019-02-01"),
             rec("c", dur=205.0, date="2019-03-01")]
        res = collapse(r)
        self.assertEqual(ids(res), ["a", "c"])  # 200 ~ 202.5 and 202.5 ~ 205, but 200 !~ 205
        r = [rec(str(i), dur=200.0 + 2.5 * i, date=f"2019-01-{i + 1:02d}") for i in range(10)]
        res = collapse(r)
        kept = [x["duration"] for x in res.kept]
        self.assertGreater(len(kept), 1)
        for x, y in zip(kept, kept[1:]):
            self.assertGreater(abs(x - y), 3)

    def test_dropped_is_within_tolerance_of_its_kept_record(self):
        random.seed(7)
        r = [rec(str(i), dur=180 + random.random() * 30, date=f"2019-{1 + i % 12:02d}-{1 + i % 28:02d}") for i in range(300)]
        by_id = {x["song_id"]: x for x in r}
        res = collapse(r, collect_pairs=True)
        self.assertEqual(len(res.pairs), res.dropped)
        for dropped, kept in res.pairs:
            self.assertLessEqual(abs(by_id[dropped]["duration"] - by_id[kept]["duration"]), 3.0 + 1e-6)

    def test_a_longer_edit_is_a_different_recording(self):
        r = [rec("a", dur=200.0), rec("b", dur=285.0, date="2020-01-01")]  # extended mix, no tag in the title
        self.assertEqual(ids(collapse(r)), ["a", "b"])

    def test_missing_or_bad_durations_are_kept_and_counted(self):
        bad = [None, 0, 0.0, -5, float("nan"), float("inf"), True, "200", [200]]
        r = [rec("ok1")] + [rec(f"b{i}", dur=d) for i, d in enumerate(bad)] + [rec("ok2", date="2020-01-01")]
        r.append(rec("nodur"))
        del r[-1]["duration"]
        res = collapse(r)
        self.assertEqual(ids(res), ["ok1"] + [f"b{i}" for i in range(len(bad))] + ["nodur"])
        self.assertEqual(res.uncomparable, len(bad) + 1)
        self.assertEqual(res.dropped, 1)

    def test_missing_title_or_artist_is_kept(self):
        r = [rec("a", name=None), rec("b", name=""), rec("c", name="   "), rec("d", artists=None, album_artist=None),
             rec("e", name="!!!"), rec("f", name="!!!", date="2020-01-01")]
        res = collapse(r)
        self.assertEqual(ids(res), ["a", "b", "c", "d", "e"])  # "!!!" has a (punctuation-only) key, e and f merge
        self.assertEqual(res.uncomparable, 4)

    def test_custom_tolerance(self):
        r = [rec("a", dur=200.0), rec("b", dur=206.0, date="2020-01-01")]
        self.assertEqual(len(collapse(r).kept), 2)
        self.assertEqual(len(collapse(r, tolerance=6).kept), 1)


class Properties(unittest.TestCase):
    def sample(self):
        random.seed(1)
        out = []
        for i in range(400):
            n = random.randint(0, 40)
            out.append(rec(f"id{i}", random.choice(["Song %d", "Song %d (Remastered 2011)", "Song %d - Live", "Song %d (Explicit)"]) % n,
                           artist=random.choice(["A", "B"]), dur=150 + random.randint(0, 24) * 2.5,
                           rtype=random.choice(["album", "single", "compilation", None]),
                           date=random.choice(["2018-01-01", "2019", "2019-06", "2020-02-02", None]),
                           explicit=random.choice([True, False, None])))
        return out

    def test_idempotent(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                once = collapse(self.sample(), merge_variants=flag)
                twice = collapse(once.kept, merge_variants=flag)
                self.assertEqual([r["song_id"] for r in twice.kept], ids(once))
                self.assertEqual(twice.dropped, 0)
                self.assertLess(once.unique, once.listed)

    def test_input_not_mutated(self):
        r = self.sample()
        before = copy.deepcopy(r)
        collapse(r, collect_pairs=True)
        self.assertEqual(r, before)

    def test_order_is_input_order_and_independent_of_rank(self):
        r = [rec("z", "B Song", date="2020-01-01"), rec("y", "A Song", date="2019-01-01"),
             rec("x", "B Song", date="2018-01-01")]
        self.assertEqual(ids(collapse(r)), ["y", "x"])

    def test_deterministic(self):
        r = self.sample()
        self.assertEqual(ids(collapse(r)), ids(collapse(list(r))))

    def test_performance_10k(self):
        random.seed(3)
        r = [rec(f"id{i}", f"Song {i % 3000}", artist=f"Art {i % 7}", dur=100 + random.random() * 200,
                 date=f"20{10 + i % 12}-01-01") for i in range(10000)]
        t = time.perf_counter()
        collapse(r)
        self.assertLess(time.perf_counter() - t, 3.0)

    def test_performance_10k_in_one_group(self):
        r = [rec(f"id{i}", "Same", dur=1.0 + (i % 5000) * 0.1, date=f"20{10 + i % 12}-01-01") for i in range(10000)]
        t = time.perf_counter()
        res = collapse(r)
        self.assertLess(time.perf_counter() - t, 3.0)
        self.assertLess(res.unique, 600)  # about 500 s / 3 s of distinct durations... bounded, not 10000

    def test_empty_and_bad_input(self):
        res = collapse([])
        self.assertEqual((res.listed, res.unique, res.dropped, res.kept), (0, 0, 0, []))
        with self.assertRaises(TypeError):
            collapse([rec("a"), "nope"])


def _child(conn, fn, args):
    conn.send(fn(*args))


def within(seconds, fn, *args):
    """Runs fn(*args) in a forked child and kills it after `seconds`: a regex blocking in C cannot be interrupted
    by a signal handler, so a slow (quadratic) implementation fails the test instead of hanging the suite."""
    ctx = multiprocessing.get_context("fork")
    recv, send = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(send, fn, args))
    p.start()
    if not recv.poll(seconds):
        p.kill()
        p.join()
        raise AssertionError(f"exceeded the {seconds} s budget")
    out = recv.recv()
    p.join()
    return out


class LongText(unittest.TestCase):
    SHAPES = {
        "spaces": " " * 100_000 + "x",
        "dash_spaces": "-" + " " * 100_000,
        "paren_spaces": "(" + " " * 100_000,
        "dashes": " - " * 50_000,
        "a_dashes": "a - " * 30_000,
        "brackets": "(a " * 40_000,
    }

    def test_pathological_titles_return_fast_and_are_uncomparable(self):
        for shape, title in self.SHAPES.items():
            with self.subTest(shape=shape):
                res = within(1, collapse, [rec("a", title), rec("b", title, date="2020-01-01")])
                self.assertEqual(ids(res), ["a", "b"])
                self.assertEqual((res.uncomparable, res.dropped), (2, 0))
                self.assertEqual(within(1, title_key, title), ("", False, False))

    def test_long_artist_is_uncomparable(self):
        r = [rec("a", artist=" " * 100_000 + "x"), rec("b", artist=" " * 100_000 + "x", date="2020-01-01")]
        res = within(1, collapse, r)
        self.assertEqual((ids(res), res.uncomparable), (["a", "b"], 2))

    def test_bound_is_inclusive(self):
        at, over = "a" * dedupe.MAX_TEXT_LEN, "a" * (dedupe.MAX_TEXT_LEN + 1)
        res = collapse([rec("a", at), rec("b", at, date="2020-01-01")])
        self.assertEqual((ids(res), res.uncomparable), (["a"], 0))
        res = collapse([rec("a", over), rec("b", over, date="2020-01-01")])
        self.assertEqual((ids(res), res.uncomparable), (["a", "b"], 2))

    def test_worst_case_at_the_bound_is_fast(self):
        n = dedupe.MAX_TEXT_LEN
        for title in (" " * n, "(" + " " * (n - 1), " - " * (n // 3), "a - " * (n // 4)):
            within(1, title_key, title)

    def test_many_titles_at_the_bound_stay_fast(self):
        r = [rec(f"id{i}", " - " * 100 + str(i), dur=100 + i % 200) for i in range(10000)]
        within(5, collapse, r)


class Report(unittest.TestCase):
    def test_pair_ids_are_sanitised(self):
        res = collapse([rec("a\nFAKE line\x1b[0m"), rec("b\r\nx", date="2020-01-01")], collect_pairs=True)
        lines = []
        dedupe.report(res, out=lines.append, show_pairs=True)
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertTrue(ln.isprintable(), ln)
        self.assertEqual(lines[1], "dropped b??x -> kept a?FAKE line?[0m")

    def test_summary_and_counts_only_by_default(self):
        res = collapse([rec("a", "Secret Title"), rec("b", "Secret Title", date="2020-01-01"), rec("c", "Other")],
                       collect_pairs=True)
        lines = []
        dedupe.report(res, out=lines.append)
        self.assertEqual(lines, ["listed 3, unique 2, dropped 1"])
        lines.clear()
        dedupe.report(res, out=lines.append, show_pairs=True)
        self.assertEqual(lines, ["listed 3, unique 2, dropped 1", "dropped b -> kept a"])
        self.assertTrue(all("Secret" not in ln for ln in lines))

    def test_collapse_prints_nothing(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            collapse([rec("a"), rec("b")])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_pairs_only_when_asked(self):
        r = [rec("a"), rec("b", date="2020-01-01")]
        self.assertEqual(collapse(r).pairs, [])
        self.assertEqual(collapse(r, collect_pairs=True).pairs, [("b", "a")])


class DropStored(unittest.TestCase):
    """Cross-source dedupe against rows read from D1: (title, primary artist, duration_s)."""

    def drop(self, records, rows):
        return dedupe.drop_stored(records, rows)

    def test_exact_match_is_dropped_and_input_is_untouched(self):
        r = [rec("lfm:" + "a" * 16, "Song", "Art", 200)]
        before = copy.deepcopy(r)
        res = self.drop(r, [("Song", "Art", 200)])
        self.assertEqual((res.new, res.listed, res.stored, res.stored_without_duration), ([], 1, 1, 0))
        self.assertEqual(r, before)

    def test_normalised_artist_and_tag_rules(self):
        res = self.drop([rec("a", "Song - Remastered 2011", "ART!", 201.5)], [("Song (Deluxe Edition)", "art", 200)])
        self.assertEqual(res.new, [])

    def test_different_artist_or_title_stays(self):
        r = [rec("a", "Song", "Other", 200), rec("b", "Other Song", "Art", 200)]
        self.assertEqual(ids_of(self.drop(r, [("Song", "Art", 200)])), ["a", "b"])

    def test_duration_beyond_tolerance_stays_within_is_dropped(self):
        r = [rec("a", dur=203.0), rec("b", dur=203.1), rec("c", dur=197.0), rec("d", dur=196.9)]
        self.assertEqual(ids_of(self.drop(r, [("Song", "Art", 200)])), ["b", "d"])

    def test_several_stored_durations_in_any_order(self):
        rows = [("Song", "Art", 300), ("Song", "Art", 100), ("Song", "Art", 200)]
        r = [rec("a", dur=201), rec("b", dur=150), rec("c", dur=299), rec("d", dur=101), rec("e", dur=250)]
        self.assertEqual(ids_of(self.drop(r, rows)), ["b", "e"])

    def test_live_and_explicit_variants_stay_distinct(self):
        r = [rec("a", "Song - Live", "Art", 200), rec("b", "Song (Live at X)", "Art", 200),
             rec("c", "Song", "Art", 200, explicit=True)]
        self.assertEqual(ids_of(self.drop(r, [("Song", "Art", 200)])), ["a", "b", "c"])
        self.assertEqual(self.drop([rec("a", "Song - Live", "Art", 200)], [("Song (Live)", "Art", 200)]).new, [])

    def test_record_without_duration_matches_on_artist_and_title(self):
        r = [rec("a", dur=None), rec("b", dur=0), rec("c", "Nope", dur=None)]
        res = self.drop(r, [("Song", "Art", 200)])
        self.assertEqual((ids_of(res), res.stored, res.stored_without_duration), (["c"], 2, 2))

    def test_stored_row_without_duration_matches_on_artist_and_title(self):
        res = self.drop([rec("a", dur=500)], [("Song", "Art", None)])
        self.assertEqual((res.new, res.stored_without_duration), ([], 1))
        res = self.drop([rec("a", dur=500)], [("Song", "Art", None), ("Song", "Art", 500)])
        self.assertEqual((res.new, res.stored_without_duration), ([], 0))  # matched by duration

    def test_uncomparable_records_and_rows_never_match(self):
        r = [rec("a", "", "Art", 200), rec("b", "Song", "", 200, artists=[], album_artist=""),
             rec("c", "x" * 400, "Art", 200)]
        res = self.drop(r, [("", "Art", 200), ("Song", "", 200), ("x" * 400, "Art", 200), (None, "Art", 1), ("Song", 3, 1)])
        self.assertEqual(ids_of(res), ["a", "b", "c"])

    def test_empty_inputs(self):
        self.assertEqual(self.drop([], [("Song", "Art", 200)]).listed, 0)
        r = [rec("a")]
        self.assertEqual(self.drop(r, []).new, r)

    def test_non_dict_record_raises(self):
        with self.assertRaises(TypeError):
            self.drop(["x"], [])

    def test_order_kept_and_many_stored_rows_are_fast(self):
        rows = [(f"Title {i}", f"Artist {i % 500}", 100 + i % 300) for i in range(20000)]
        records = [rec(f"n{i}", f"Title {i * 7}", f"Artist {(i * 7) % 500}", 100 + (i * 7) % 300) for i in range(2000)]
        records += [rec(f"u{i}", f"Unseen {i}", "Artist 1", 150) for i in range(2000)]
        t = time.monotonic()
        res = self.drop(records, rows)
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(ids_of(res), [f"u{i}" for i in range(2000)])  # all n* are stored (i * 7 < 20000), order kept
        self.assertEqual(res.stored, 2000)


def ids_of(result):
    return [r["song_id"] for r in result.new]


if __name__ == "__main__":
    unittest.main()

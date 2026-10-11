"""Relaxed title normalisation of the matcher (#194). Synthetic and well-known public titles only. Offline: the fake
YouTube Music client never touches a network."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import dedupe
import ingest
import match
from test_ingest import Fakes, SONG, sid


def res(title, artists=("Queen",), dur=200, vid="v1"):
    return {"videoId": vid, "title": title, "duration_seconds": dur, "artists": [{"name": a} for a in artists]}


class FakeYtm:
    def __init__(self, per_query=None, default=()):
        self.per_query, self.default, self.calls = per_query or {}, list(default), []

    def search(self, q, filter=None, limit=None):
        self.calls.append((q, filter, limit))
        return list(self.per_query.get(q, self.default))


def same(a, b):
    return match.relaxed_similarity(a, b) >= match.TITLE_MIN_SIMILARITY


class NoiseTests(unittest.TestCase):
    def test_each_noise_form_is_dropped(self):
        for noisy in ("Bohemian Rhapsody - Remastered 2011", "Bohemian Rhapsody (Remastered 2011)",
                      "Bohemian Rhapsody [Remastered]", "Bohemian Rhapsody - 2011 Remaster",
                      "Bohemian Rhapsody (Remastered Version)", "Bohemian Rhapsody (1975)",
                      "Bohemian Rhapsody (Album Version)", "Bohemian Rhapsody - Single Version",
                      "Bohemian Rhapsody [Explicit]", "Bohemian Rhapsody (Explicit)",
                      "Bohemian Rhapsody (Remastered 2011) [Explicit]"):
            self.assertEqual(match.title_parts(noisy), (["bohemian", "rhapsody"], ()), noisy)
            self.assertEqual(match.relaxed_similarity("Bohemian Rhapsody", noisy), 1.0, noisy)

    def test_noise_inside_a_longer_group_or_title_is_kept(self):
        self.assertNotEqual(match.title_parts("Song (Remastered Live)")[0], ["song"])
        self.assertNotEqual(match.title_parts("Song (feat. Someone) (1975 Mix)")[0], ["song"])

    def test_a_title_that_is_only_noise_is_not_emptied(self):
        self.assertEqual(match.title_parts("(1999)")[0], ["1999"])
        self.assertEqual(match.title_parts("1999 - Remastered")[0], ["1999"])

    def test_ampersand_and_and(self):
        self.assertEqual(match.relaxed_similarity("Fire & Ice", "Fire and Ice"), 1.0)
        self.assertEqual(match.relaxed_similarity("Fire and Ice", "Fire & Ice"), 1.0)

    def test_apostrophes_case_diacritics_punctuation(self):
        self.assertEqual(match.relaxed_similarity("Don't Stop Me Now", "Dont Stop Me Now"), 1.0)
        self.assertEqual(match.relaxed_similarity("Don’t Stop", "DON'T STOP!"), 1.0)
        self.assertEqual(match.relaxed_similarity("Cafe del Mar", "Café del Mar"), 1.0)
        self.assertEqual(match.relaxed_similarity("Angel, Of Mine", "Angel of Mine"), 1.0)

    def test_cyrillic_breve_is_not_stripped(self):
        self.assertEqual(match.title_parts("Май")[0], ["май"])
        self.assertEqual(match.relaxed_similarity("Дойду", "Доиду") < 1.0, True)

    def test_plural_of_the_last_word_only(self):
        self.assertEqual(match.relaxed_similarity("Sweet Dreams", "Sweet Dream"), 1.0)
        # only the LAST word: a plural in the first word is a plain difference (never made equal)
        self.assertLess(match.relaxed_similarity("Dreams Sweet", "Dream Sweet"), 1.0)
        self.assertEqual(match._singular_last(["dreams", "sweet"]), ["dreams", "sweet"])
        self.assertEqual(match._singular_last(["dreams", "come", "true"]), ["dreams", "come", "true"])
        self.assertEqual(match._singular_last(["sweet", "dreams"]), ["sweet", "dream"])
        self.assertEqual(match._singular_last(["dreams", "comes"]), ["dreams", "come"])  # first word untouched
        self.assertEqual(match.relaxed_similarity("Love", "Loves"), match.similarity("Love", "Loves"))  # one word: no rule
        self.assertLess(match.relaxed_similarity("Bad Bus", "Bad Bu"), 1.0)  # 'us' / 'is' / 'ss' / short words are untouched
        self.assertEqual(match._singular_last(["the", "boys"]), ["the", "boy"])
        for w in ("class", "bus", "this", "was", "yes"):
            self.assertEqual(match._singular_last(["a", w]), ["a", w], w)


MARKERS = ["live", "acoustic", "remix", "instrumental", "karaoke", "cover", "sped up", "slowed", "8D", "reverb",
           "radio edit", "extended", "demo", "live at Wembley"]


class MarkerGuardTests(unittest.TestCase):
    def test_a_marker_never_matches_the_title_without_it(self):
        for m in MARKERS:
            for form in (f"Under Pressure ({m})", f"Under Pressure - {m}", f"Under Pressure [{m}]"):
                self.assertFalse(same("Under Pressure", form), form)
                self.assertFalse(same(form, "Under Pressure"), form)

    def test_marker_blocks_even_with_a_long_title_and_noise(self):
        base = "The Ballad Of Many Words Which Goes On For Quite A While Here And Even Longer Than You Think"
        for m in MARKERS:
            self.assertFalse(same(base, f"{base} ({m}) - Remastered 2011"), m)
            self.assertFalse(same(f"{base} - Remastered 2011", f"{base} ({m})"), m)

    def test_two_different_markers_never_match(self):
        for i, a in enumerate(MARKERS):
            for b in MARKERS[i + 1:]:
                self.assertFalse(same(f"Under Pressure ({a})", f"Under Pressure ({b})"), (a, b))

    def test_live_venues_stay_distinct(self):
        self.assertFalse(same("Song (Live at Wembley)", "Song (Live at Budokan)"))
        self.assertFalse(same("Song (Live)", "Song (Live at Wembley)"))

    def test_live_with_and_without_a_venue_or_year_on_a_long_title(self):
        base = "The Ballad Of Many Words Which Goes On For Quite A While Here And Even Longer Than You Think"
        self.assertFalse(same(f"{base} (Live)", f"{base} (Live at Wembley)"))
        self.assertTrue(same(f"{base} (Live at Wembley)", f"{base} - Live in Wembley"))
        self.assertFalse(same("Song (Live)", "Song - Live 2005"))  # a year in the marker is part of it
        self.assertTrue(same("Song - Live 2005", "Song (Live 2005)"))

    def test_a_marker_in_the_title_is_not_cancelled_by_a_second_one(self):
        self.assertFalse(same("I Live In Fear", "I Live In Fear (Live)"))
        self.assertTrue(same("I Live In Fear", "I Live In Fear - Remastered 2011"))

    def test_the_same_marker_spelled_or_ordered_differently_matches(self):
        for a, b in (("Song (Live)", "Song - Live"), ("Song [Acoustic]", "Song (Acoustic)"),
                     ("Song - Radio Edit", "Song (Radio Edit)"), ("Song (Acoustic Version)", "Song - Acoustic"),
                     ("Song (Live at Wembley)", "Song - Live in Wembley"),
                     ("Song (live in Wembley 2005) (acoustic)", "Song - Acoustic / Live At Wembley, 2005"),
                     ("Song (Sped Up)", "Song - sped up"), ("Song (8D Audio)", "Song [8D Audio]")):
            self.assertTrue(same(a, b), (a, b))
            self.assertTrue(same(b, a), (b, a))

    def test_the_guard_covers_the_dedupe_variants(self):
        self.assertTrue(dedupe._NOT_SAME <= match._RELAXED_MARKERS)
        self.assertTrue({"live", "extended", "radio", "8d", "reverb"} <= match._RELAXED_MARKERS)


class NoPrefixRuleTests(unittest.TestCase):
    PAIRS = [("Bye Bye", "Bye Bye Bye"), ("No No", "No No No"), ("Na Na", "Na Na Na"), ("Baby Baby", "Baby Baby Baby"),
             ("Hey Ya", "Hey Ya Ya Ya"), ("Hold On", "Hold On Hold On"), ("Hello Goodbye", "Hello Goodbye Hello"),
             ("Go Go Go", "Go Go"), ("Love", "Love Story"), ("Love Me", "Love Me Do"), ("La", "La La La"),
             ("Ooh La", "Ooh La La La")]

    def test_prefix_titles_stay_no_match_in_both_modes_and_directions(self):
        for a, b in self.PAIRS:
            for x, y in ((a, b), (b, a)):
                self.assertFalse(same(x, y), (x, y))
                for dur in (200, None):
                    self.assertIsNone(match.find_match(FakeYtm(default=[res(y)]), x, "Queen", None, dur), (x, y, dur))


class ApostropheTests(unittest.TestCase):
    def test_one_word_titles_keep_the_apostrophe(self):
        for a, b in (("Hell", "He'll"), ("Well", "We'll"), ("Ill", "I'll"), ("Id", "I'd"), ("Were", "We're"),
                     ("Shell", "She'll")):
            for x, y in ((a, b), (b, a)):
                self.assertNotEqual(match.title_parts(x)[0], match.title_parts(y)[0], (x, y))
                self.assertLess(match.relaxed_similarity(x, y), match.TITLE_ONLY_TITLE_MIN_SIMILARITY, (x, y))
                self.assertIsNone(match.find_match(FakeYtm(default=[res(y)]), x, "Queen", None, None), (x, y))

    def test_longer_titles_ignore_the_apostrophe(self):
        self.assertEqual(match.relaxed_similarity("Dont Stop", "Don't Stop"), 1.0)
        self.assertEqual(match.relaxed_similarity("Don\u2019t Stop", "Dont Stop"), 1.0)


class BoundsTests(unittest.TestCase):
    def test_a_huge_title_is_left_to_the_strict_pass_and_is_fast(self):
        import time
        for title in ("Song" + " " * 20000 + "(Remastered)", "(" * 20000, "Song - " + " " * 20000):
            t0 = time.monotonic()
            self.assertEqual(match.relaxed_similarity("Song", title), 0.0)
            self.assertEqual(match.relaxed_similarity(title, title), 0.0)
            self.assertLess(time.monotonic() - t0, 1.0)

    def test_a_title_at_the_cap_is_still_processed(self):
        t = "a" * match.MAX_RELAXED_TITLE_LEN
        self.assertEqual(match.relaxed_similarity(t, t), 1.0)

    def test_live_years_and_venues_must_be_equal(self):
        self.assertFalse(same("Live At A 1999", "Live At A 2000"))
        self.assertFalse(same("Song (Live at A 1999)", "Song (Live at A 2000)"))
        self.assertTrue(same("Song (Live at A 1999)", "Song - Live in A 1999"))
        for dur in (None, 200):
            self.assertIsNone(match.find_match(FakeYtm(default=[res("Some Live At A 2000")]), "Some Live At A 1999", "Queen", None, dur))


class RelaxedFlagTests(unittest.TestCase):
    def test_flag_only_on_relaxed_matches(self):
        for dur in (200, None):
            strict = match.find_match(FakeYtm(default=[res("Some Song")]), "Some Song", "Queen", None, dur)
            self.assertFalse(strict.relaxed)
            relaxed = match.find_match(FakeYtm(default=[res("Some Song (Remastered 2011)")]), "Some Song", "Queen", None, dur)
            self.assertTrue(relaxed.relaxed)
        self.assertFalse(match.score("Some Song", "Queen", 200, res("Some Song")).relaxed)
        self.assertTrue(match.score("Some Song", "Queen", 200, res("Some Song [Explicit]"), relaxed=True).relaxed)


class ScoringTests(unittest.TestCase):
    def test_duration_tolerance_is_unchanged(self):
        self.assertEqual(match.DURATION_TOLERANCE_S, 3)
        r = res("Bohemian Rhapsody - Remastered 2011", dur=203)
        self.assertIsNotNone(match.score("Bohemian Rhapsody", "Queen", 200, r, relaxed=True))
        r = res("Bohemian Rhapsody - Remastered 2011", dur=204)
        self.assertIsNone(match.score("Bohemian Rhapsody", "Queen", 200, r, relaxed=True))

    def test_title_only_thresholds_are_unchanged(self):
        self.assertEqual((match.TITLE_ONLY_TITLE_MIN_SIMILARITY, match.TITLE_ONLY_ARTIST_MIN_SIMILARITY,
                          match.TITLE_ONLY_MIN_DURATION_S, match.TITLE_ONLY_MAX_DURATION_S), (0.93, 0.7, 60, 600))
        self.assertEqual(match.TITLE_MIN_SIMILARITY, 0.85)

    def test_title_only_relaxed_keeps_the_threshold_artist_and_range(self):
        ok = match.score_title_only("Sweet Dreams", "Queen", res("Sweet Dream"), relaxed=True)
        self.assertEqual(ok.mode, match.MODE_TITLE_ONLY)
        self.assertIsNone(match.score_title_only("Sweet Dreams", "Queen", res("Sweet Dream", ("Other Band",)), relaxed=True))
        self.assertIsNone(match.score_title_only("Sweet Dreams", "Queen", res("Sweet Dream", dur=30), relaxed=True))
        self.assertIsNone(match.score_title_only("Sweet Dreams", "Queen", res("Sweet Dream", dur=601), relaxed=True))
        # 0.923 stays below the 0.93 line (one letter in a 13 letter title)
        self.assertLess(match.relaxed_similarity("Sweet Dreamer", "Sweet Dreamex"), 0.93)
        self.assertIsNone(match.score_title_only("Sweet Dreamer", "Queen", res("Sweet Dreamex"), relaxed=True))


class FindMatchTests(unittest.TestCase):
    def find(self, ytm, title, artist="Queen", dur=200):
        return match.find_match(ytm, title, artist, None, dur)

    def test_relaxed_pass_finds_the_remastered_title(self):
        ytm = FakeYtm(default=[res("Bohemian Rhapsody - Remastered 2011", dur=201)])
        c = self.find(ytm, "Bohemian Rhapsody")
        self.assertEqual((c.video_id, c.mode), ("v1", match.MODE_DURATION))

    def test_every_noise_class_matches_end_to_end(self):
        for noisy in ("Some Song (Remastered 2011)", "Some Song (1975)", "Some Song (Album Version)",
                      "Some Song (Single Version)", "Some Song [Explicit]"):
            self.assertIsNotNone(self.find(FakeYtm(default=[res(noisy)]), "Some Song"), noisy)
            self.assertIsNotNone(self.find(FakeYtm(default=[res(noisy)]), "Some Song", dur=None), noisy)

    def test_search_calls_do_not_change(self):
        for results, expect_hit in (([res("Some Song")], True), ([res("Some Song (Live)")], False),
                                    ([res("Some Song (Explicit)")], True)):
            ytm = FakeYtm(default=results)
            self.assertEqual(self.find(ytm, "Some Song") is not None, expect_hit)
            self.assertEqual(len(ytm.calls), 1 if expect_hit and results[0]["title"] == "Some Song" else 3)
            self.assertTrue(all(c[1:] == ("songs", 10) for c in ytm.calls))

    def test_strict_candidate_wins_over_a_relaxed_only_one(self):
        ytm = FakeYtm(default=[res("Some Song (Explicit)", vid="relaxed"), res("Some Song", vid="strict", dur=202)])
        self.assertEqual(self.find(ytm, "Some Song").video_id, "strict")

    def test_a_marker_candidate_is_never_returned_for_a_plain_title(self):
        for m in MARKERS:
            for dur in (200, None):
                self.assertIsNone(self.find(FakeYtm(default=[res(f"Some Song ({m}) [Explicit]")]), "Some Song", dur=dur), m)

    def test_different_artist_is_rejected_in_title_only_mode(self):
        self.assertIsNone(self.find(FakeYtm(default=[res("Some Song (Explicit)", ("Unrelated Band",))]), "Some Song", dur=None))

    def test_love_is_not_love_story(self):
        for dur in (200, None):
            self.assertIsNone(self.find(FakeYtm(default=[res("Love Story")]), "Love", dur=dur))


class PipelineFlagTests(unittest.TestCase):
    """The relaxed-pass marker reaches the log, the counts, the summary and the result JSON; strict matches show none."""
    URL = "https://open.spotify.com/playlist/" + "b" * 22

    def run_one(self, cand):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            f = Fakes(Path(d))
            deps = f.deps()
            deps.find_match = lambda s: cand
            counts = ingest.run([dict(SONG, song_id=sid(1), url="u")], deps, set(), {}, Path(d), False)
        return counts, out.getvalue()

    def test_relaxed_match_is_marked(self):
        counts, log = self.run_one(match.Candidate("v", "T", "A", 200, 2.0, relaxed=True))
        self.assertEqual((counts["new"], counts["matched_normalized"]), (1, 1))
        self.assertIn("(matched after title normalization)", log)
        self.assertIn("1 matched after title normalization", ingest.summary(counts))

    def test_strict_match_has_no_marker_no_key(self):
        counts, log = self.run_one(match.Candidate("v", "T", "A", 200, 2.0))
        self.assertNotIn("matched_normalized", counts)
        self.assertNotIn("normalization", log)
        self.assertNotIn("normalization", ingest.summary(counts))

    def test_a_non_candidate_object_is_never_marked(self):
        counts, log = self.run_one(object())
        self.assertNotIn("matched_normalized", counts)

    def test_result_json_is_additive(self):
        c = {"new": 2, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0, "failed": 0, "cooldown": 0,
             "matched_normalized": 2}
        r = ingest.build_result([self.URL], {self.URL: c}, None, 0)
        self.assertEqual(r["counts"]["matched_normalized"], 2)
        self.assertEqual(r["playlists"][0]["matched_normalized"], 2)
        r2 = ingest.build_result([self.URL], {self.URL: dict(c, matched_normalized=0)}, None, 0)
        self.assertNotIn("matched_normalized", r2["counts"])
        self.assertNotIn("matched_normalized", r2["playlists"][0])


if __name__ == "__main__":
    unittest.main()

"""Relaxed title normalisation of the matcher (#194). Synthetic and well-known public titles only. Offline: the fake
YouTube Music client never touches a network."""
import unittest

import dedupe
import match


def res(title, artists=("Queen",), dur=200, vid="v1"):
    return {"videoId": vid, "title": title, "duration_seconds": dur, "artists": [{"name": a} for a in artists]}


class FakeYtm:
    def __init__(self, per_query=None, default=()):
        self.per_query, self.default, self.calls = per_query or {}, list(default), []

    def search(self, q, filter=None, limit=None):
        self.calls.append((q, filter, limit))
        return list(self.per_query.get(q, self.default))


def same(a, b, prefix=False):
    return match.relaxed_similarity(a, b, allow_prefix=prefix) >= match.TITLE_MIN_SIMILARITY


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
        self.assertLess(match.relaxed_similarity("Dreams Sweet", "Dream Sweet"), 1.0)  # not the first word
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
        self.assertTrue(same("Song (Live)", "Song - Live 2005"))

    def test_a_marker_in_the_title_is_not_cancelled_by_a_second_one(self):
        self.assertFalse(same("I Live In Fear", "I Live In Fear (Live)"))
        self.assertTrue(same("I Live In Fear", "I Live In Fear - Remastered 2011"))

    def test_the_same_marker_spelled_or_ordered_differently_matches(self):
        for a, b in (("Song (Live)", "Song - Live"), ("Song [Acoustic]", "Song (Acoustic)"),
                     ("Song - Radio Edit", "Song (Radio Edit)"), ("Song (Acoustic Version)", "Song - Acoustic"),
                     ("Song (Live at Wembley)", "Song - Live in Wembley"),
                     ("Song (live in Wembley) (acoustic)", "Song - Acoustic / Live At Wembley, 2005"),
                     ("Song (Sped Up)", "Song - sped up"), ("Song (8D Audio)", "Song [8D Audio]")):
            self.assertTrue(same(a, b), (a, b))
            self.assertTrue(same(b, a), (b, a))

    def test_the_guard_covers_the_dedupe_variants(self):
        self.assertTrue(dedupe._NOT_SAME <= match._RELAXED_MARKERS)
        self.assertTrue({"live", "extended", "radio", "8d", "reverb"} <= match._RELAXED_MARKERS)


class PrefixTests(unittest.TestCase):
    def test_repeated_chant_prefix_matches_on_the_duration_path_only(self):
        self.assertTrue(same("Ooh La", "Ooh La La La", prefix=True))
        self.assertTrue(same("Ooh La La La", "Ooh La", prefix=True))
        self.assertFalse(same("Ooh La", "Ooh La La La"))

    def test_conservative_prefix_guards(self):
        self.assertFalse(same("Love", "Love Story", prefix=True))        # one word
        self.assertFalse(same("La", "La La La", prefix=True))            # one word
        self.assertFalse(same("Love Me", "Love Me Do", prefix=True))     # extra word is new
        self.assertFalse(same("Hey Jude", "Hey Jude Again Today", prefix=True))
        self.assertFalse(same("Ooh La", "La Ooh La La", prefix=True))    # not a start


class ScoringTests(unittest.TestCase):
    def test_duration_tolerance_is_unchanged(self):
        self.assertEqual(match.DURATION_TOLERANCE_S, 3)
        r = res("Bohemian Rhapsody - Remastered 2011", dur=203)
        self.assertIsNotNone(match.score("Bohemian Rhapsody", "Queen", 200, r, relaxed=True))
        r = res("Bohemian Rhapsody - Remastered 2011", dur=204)
        self.assertIsNone(match.score("Bohemian Rhapsody", "Queen", 200, r, relaxed=True))

    def test_title_only_thresholds_are_unchanged(self):
        self.assertEqual((match.TITLE_ONLY_TITLE_MIN_SIMILARITY, match.TITLE_ONLY_ARTIST_MIN_SIMILARITY,
                          match.TITLE_ONLY_MIN_DURATION_S, match.TITLE_ONLY_MAX_DURATION_S), (0.93, 0.6, 60, 600))
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
        # the prefix rule is not part of title-only
        self.assertIsNone(match.score_title_only("Ooh La", "Queen", res("Ooh La La La"), relaxed=True))


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

    def test_prefix_match_end_to_end(self):
        self.assertIsNotNone(self.find(FakeYtm(default=[res("Ooh La La La")]), "Ooh La"))
        self.assertIsNone(self.find(FakeYtm(default=[res("Ooh La La La")]), "Ooh La", dur=None))


if __name__ == "__main__":
    unittest.main()

"""Title-and-artist-only matcher mode (#151): tracks without a usable duration. The duration path must not change."""
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
    """search(q, filter=, limit=) -> the same result list for every query; records the calls."""
    def __init__(self, results):
        self.results, self.calls = results, []

    def search(self, q, filter=None, limit=None):
        self.calls.append((q, filter, limit))
        return list(self.results)


def find(results, title="Under Pressure", artist="Queen", duration=None, album=None):
    return match.find_match(FakeYtm(results), title, artist, album, duration)


class TitleOnlyTest(unittest.TestCase):
    def test_accepts_clear_title_and_artist_match(self):
        c = find([res("Under Pressure")])
        self.assertEqual((c.video_id, c.duration_s, c.mode), ("v1", 200, match.MODE_TITLE_ONLY))

    def test_zero_and_none_use_the_mode(self):
        for d in (None, 0):
            self.assertEqual(find([res("Under Pressure")], duration=d).mode, "title-only")

    def test_searches_the_songs_filter_only(self):
        ytm = FakeYtm([res("Other Song")])
        self.assertIsNone(match.find_match(ytm, "Under Pressure", "Queen", None, None))
        self.assertTrue(ytm.calls and all(f == "songs" for _, f, _ in ytm.calls))

    def test_rejects_live_remix_cover_and_friends(self):
        for bad in ("Under Pressure (Live)", "Under Pressure - Live at Wembley", "Under Pressure (Remix)",
                    "Under Pressure (Acoustic)", "Under Pressure Instrumental", "Under Pressure (Karaoke Version)",
                    "Under Pressure (Cover)", "Under Pressure (Sped Up)", "Under Pressure (Slowed)",
                    "Under Pressure Nightcore"):
            self.assertIsNone(find([res(bad)]), bad)

    def test_variant_word_in_the_requested_title_is_allowed(self):
        c = find([res("Live and Let Die", ("Wings",))], title="Live and Let Die", artist="Wings")
        self.assertIsNotNone(c)
        c = find([res("Song (Remix)", ("Queen",))], title="Song (Remix)")
        self.assertIsNotNone(c)

    def test_variant_words_cover_dedupe_not_same(self):
        self.assertTrue(dedupe._NOT_SAME <= match._VARIANT_WORDS)
        self.assertIn("live", match._VARIANT_WORDS)

    def test_rejects_wrong_artist(self):
        self.assertIsNone(find([res("Under Pressure", ("Some Cover Band",))]))
        self.assertIsNone(find([res("Under Pressure", ())]))

    def test_accepts_cyrillic_artist_spelling(self):
        c = find([res("Драма", ("Гриша Ургант",))], title="Драма", artist="Grisha Urgant")
        self.assertEqual(c.mode, "title-only")

    def test_collaboration_with_the_artist_among_several_is_accepted(self):
        self.assertIsNotNone(find([res("Under Pressure", ("Queen", "David Bowie"))]))

    def test_rejects_title_below_the_stricter_threshold(self):
        self.assertIsNone(find([res("Under Pressures Extended Thing")]))
        # a title the duration path (0.85) takes but title-only (0.93) must not
        t, near = "abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstu"
        self.assertTrue(match.TITLE_MIN_SIMILARITY <= match.similarity(t, near) < match.TITLE_ONLY_TITLE_MIN_SIMILARITY)
        self.assertIsNotNone(find([res(near, dur=200)], title=t, duration=200))
        self.assertIsNone(find([res(near, dur=200)], title=t))

    def test_title_threshold_boundary(self):
        base = "abcdefghijklmnopqrstuvwxyz"
        for title, ok in ((base, True), (base[:-1], True), (base[:-4], False)):
            sim = match.similarity(base, title)
            self.assertEqual(sim >= match.TITLE_ONLY_TITLE_MIN_SIMILARITY, ok, (title, sim))
            self.assertEqual(find([res(title)], title=base) is not None, ok, title)

    def test_artist_threshold_boundary(self):
        self.assertGreaterEqual(match.TITLE_ONLY_ARTIST_MIN_SIMILARITY, 0.6)
        self.assertIsNone(find([res("Under Pressure", ("Que",))], artist="Queensryche Band"))

    def test_rejects_out_of_range_duration(self):
        for d in (59, 601, 0, None, "200", True, 5000):
            self.assertIsNone(find([res("Under Pressure", dur=d)]), d)
        for d in (60, 600):
            self.assertIsNotNone(find([res("Under Pressure", dur=d)]), d)

    def test_best_candidate_is_chosen_deterministically(self):
        rs = [res("Under Pressure", ("Queen",), 200, "exact"), res("Under Pressure x", ("Queen",), 200, "near"),
              res("Under Pressure", ("Quee",), 200, "weaker artist")]
        for order in (rs, list(reversed(rs))):
            self.assertEqual(find(order).video_id, "exact")
        twin = [res("Under Pressure", vid="first"), res("Under Pressure", vid="second")]
        self.assertEqual(find(twin).video_id, find(twin).video_id)

    def test_garbage_results_are_ignored_not_raised(self):
        self.assertIsNone(find([{"videoId": "v", "title": None, "duration_seconds": 200}, {"title": "Under Pressure"}]))


class DurationPathUnchangedTest(unittest.TestCase):
    def test_title_only_quality_candidate_is_rejected_when_the_duration_differs(self):
        r = res("Under Pressure", dur=200)
        self.assertIsNotNone(find([r], duration=200))
        self.assertIsNone(find([r], duration=210))
        self.assertIsNone(find([r], duration=204))
        self.assertEqual(find([r], duration=203).mode, match.MODE_DURATION)

    def test_duration_path_ignores_artist_variants_and_range(self):
        # the old guarantees: artist never vetoes, a live title passes if close enough, no sanity range
        self.assertIsNotNone(find([res("Under Pressure", ("Nobody",), 30)], duration=30))
        self.assertIsNotNone(find([res("Under Pressure (Live)", ("Queen",), 200)], title="Under Pressure (Live)",
                                  duration=200))
        c = find([res("Under Pressure (Live)", dur=200)], duration=200)
        self.assertIsNone(c)  # title 0.85 not reached: same as before

    def test_duration_candidate_score_is_the_old_formula(self):
        r = res("Under Pressure", dur=201)
        c = match.score("Under Pressure", "Queen", 200, r)
        self.assertAlmostEqual(c.score, 1.0 + 0.3 * 1.0 - 0.02)
        self.assertEqual(c.mode, match.MODE_DURATION)


class PipelineTest(unittest.TestCase):
    """process_track / run with the fake deps: the title-only match stores the candidate duration."""
    def deps(self, f, cand):
        d = f.deps()
        d.find_match = lambda s: cand
        return d

    def lfm(self, n=1):
        s = {k: v for k, v in SONG.items() if k != "duration"}
        return dict(s, song_id=ingest.LASTFM_ID_PREFIX + f"{n:016x}", name=f"T{n}", url="", duration=None)

    def test_upsert_gets_the_candidate_duration_and_the_song_is_not_mutated(self):
        s = self.lfm()
        cand = match.Candidate("v", "T1", "A", 187, 2.0, match.MODE_TITLE_ONLY)
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d))
            notes = {}
            self.assertEqual(ingest.process_track(s, self.deps(f, cand), set(), {}, Path(d), False, notes), "new")
        self.assertEqual(f.batches[0][2][1][6], 187)
        self.assertIsNone(s["duration"])
        self.assertEqual(notes, {"title_only": True})

    def test_run_counts_and_marks_only_title_only_matches(self):
        s = self.lfm()
        cand = match.Candidate("v", "T1", "A", 187, 2.0, match.MODE_TITLE_ONLY)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            counts = ingest.run([s], self.deps(Fakes(Path(d)), cand), set(), {}, Path(d), False)
        self.assertEqual(counts["matched_title_only"], 1)
        self.assertEqual(counts["new"], 1)
        self.assertIn("(matched by title only)", out.getvalue())
        self.assertIn("1 matched by title only", ingest.summary(counts))

    def test_old_mode_has_no_marker_no_key(self):
        cand = match.Candidate("v", "T", "A", 200, 2.0)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            counts = ingest.run([dict(SONG, song_id=sid(1), url="u")], self.deps(Fakes(Path(d)), cand), set(), {},
                                Path(d), False)
        self.assertNotIn("matched_title_only", counts)
        self.assertNotIn("title only", out.getvalue())
        self.assertNotIn("title only", ingest.summary(counts))

    def test_result_json_is_additive(self):
        c = {"new": 2, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0, "failed": 0, "cooldown": 0,
             "matched_title_only": 2}
        r = ingest.build_result(["https://open.spotify.com/playlist/" + "b" * 22], {"https://open.spotify.com/playlist/" + "b" * 22: c}, None, 0)
        self.assertEqual(r["counts"]["matched_title_only"], 2)
        self.assertEqual(r["playlists"][0]["matched_title_only"], 2)
        plain = dict(c, matched_title_only=0)
        r2 = ingest.build_result(["https://open.spotify.com/playlist/" + "b" * 22], {"https://open.spotify.com/playlist/" + "b" * 22: plain}, None, 0)
        self.assertNotIn("matched_title_only", r2["counts"])
        self.assertNotIn("matched_title_only", r2["playlists"][0])

    def test_real_deps_find_picks_the_mode_by_the_duration(self):
        seen = []
        orig = ingest.find_match
        ingest.find_match = lambda ytm, t, a, al, d: seen.append(d)
        try:
            find_dep = ingest.real_deps(object()).find_match
            for dur, expected in ((200, 200), (200.7, 200), (None, None), (0, None), ("missing", None)):
                s = dict(SONG, url="u")
                if dur == "missing":
                    del s["duration"]
                else:
                    s["duration"] = dur
                find_dep(s)
                self.assertEqual(seen[-1], expected, dur)
        finally:
            ingest.find_match = orig


if __name__ == "__main__":
    unittest.main()

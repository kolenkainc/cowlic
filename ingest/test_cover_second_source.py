"""Tests for the cover backfill's second source and plan workflow (#352): the narrow name normalisation, the album-level
search with the tracklist check, `covers --dry-run`, the plan file and `covers --apply-plan`. Offline: ytmusicapi is a
stub, D1 is the in-memory SQLite of test_bootstrap_covers, no network, no .env, no real library data (every name is
invented). The bars of the guards are pinned with literal numbers on purpose, so loosening one goes red."""
import datetime
import json
import os
import stat
import unittest
from pathlib import Path

import bootstrap
import bootstrap_covers as bc
import ingest
import match
from test_bootstrap_covers import Base, FakeBackends, SENTINELS, cand
from test_lastfm_source import NoNetwork

THUMB = "https://lh3.googleusercontent.com/FAKEALBUMTHUMB=w226-h226-l90-rj"
THUMB_URL = "https://lh3.googleusercontent.com/FAKEALBUMTHUMB" + match.COVER_SIZE_SUFFIX
BID = "MPREb_fake0001"


def hit(title="Quuxnaut Album", artists=("Zorblax",), bid=BID, thumb=THUMB):
    return {"title": title, "type": "Album", "browseId": bid, "artists": [{"name": a, "id": "UCfake"} for a in artists],
            "thumbnails": [{"url": thumb, "width": 226, "height": 226}] if thumb else []}


def page(*titles, thumb=None):
    return {"tracks": [{"title": t} for t in titles], "thumbnails": [{"url": thumb}] if thumb else []}


class StubYtm:
    """ytmusicapi stand-in: `hits` maps the search query to the album results, `pages` maps a browse id to get_album."""

    def __init__(self, hits=None, pages=None):
        self.hits, self.pages = hits or {}, pages or {}
        self.searches: list = []
        self.fetches: list = []

    def search(self, query, filter=None, limit=None):
        self.searches.append((query, filter, limit))
        return self.hits.get(query, [])

    def get_album(self, browse_id):
        self.fetches.append(browse_id)
        return self.pages.get(browse_id, {"tracks": []})


def todo(album="Quuxnaut Album", artist="Zorblax", title="Frobnitz Song"):
    return bc.Todo("0123456789abcdef", album, artist, title, 200)


def level(t, ytm):
    return bc.album_level(t, ytm)


class NormalisationTest(NoNetwork):
    def narrow(self, a, b):
        return match.albums_match(a, b, narrow=True)

    def test_the_two_suffixes_are_stripped_case_insensitively_and_stacked(self):
        for stored, found in (("Foo", "Foo - Single"), ("Foo - Single", "Foo"), ("Foo [Explicit]", "Foo"),
                              ("Foo", "foo - SINGLE"), ("Foo", "Foo [EXPLICIT]"), ("Foo - Single [Explicit]", "Foo"),
                              ("Foo - Single", "Foo [Explicit]")):
            with self.subTest(stored=stored, found=found):
                self.assertTrue(self.narrow(stored, found))

    def test_the_default_guard_is_unchanged(self):
        for stored, found in (("Foo", "Foo - Single"), ("Foo [Explicit]", "Foo")):
            with self.subTest(stored=stored):
                self.assertFalse(match.albums_match(stored, found))
                self.assertFalse(match.albums_match(stored, found, narrow=False))
        self.assertTrue(match.albums_match("Foo Bar", "foo bar"))

    def test_only_a_trailing_standalone_token_is_stripped(self):
        for stored, found in (("Foo", "Single - Foo"), ("Foo", "Foo (Explicit)"), ("Foo", "Foo-Single"),
                              ("Foo", "Foo - Single Remix"), ("Foo", "Foo - Singles"), ("Foo", "Foo Explicit"),
                              ("Foo", "[Explicit] Foo")):
            with self.subTest(found=found):
                self.assertFalse(self.narrow(stored, found))

    def test_a_name_that_is_only_a_suffix_is_kept(self):
        self.assertEqual(match.strip_format_suffix("[Explicit]"), "[Explicit]")
        self.assertEqual(match.strip_format_suffix("Foo - Single"), "Foo")

    def test_the_rest_of_the_guard_still_rejects_after_the_strip(self):
        for stored, found, why in (("Foo Vol 1 - Single", "Foo Vol 2", "number"), ("Foo II [Explicit]", "Foo III", "roman"),
                                   ("Lovers - Single", "Lover", "plural"), ("Foo Deluxe - Single", "Foo", "edition"),
                                   ("Foo - Single", "Foo Live", "edition"), ("Foo", "Foo Remastered [Explicit]", "edition")):
            with self.subTest(why=why):
                self.assertFalse(self.narrow(stored, found))

    def test_the_album_ratio_bar_is_090(self):
        self.assertEqual(match.COVER_ALBUM_MIN_SIMILARITY, 0.9)
        self.assertTrue(self.narrow("abcdefghij", "abcdefghi"))      # ratio 0.947
        self.assertFalse(self.narrow("abcdefghij", "abcdefgh"))      # ratio 0.889
        self.assertFalse(match.albums_match("abcdefghij", "abcdefgh"))
        self.assertFalse(self.narrow("Alpha Beta Gamma", "Alpha Beta Delta"))

    def test_latin_against_cyrillic_is_bridged_by_the_transliteration(self):
        self.assertTrue(self.narrow("Veter Peremen", "Ветер перемен"))
        self.assertTrue(self.narrow("Ветер перемен - Single", "Veter Peremen"))
        self.assertFalse(match.albums_match("Veter Peremen", "Ветер перемен"))  # the default guard has no such step

    def test_a_wrong_album_is_not_accepted_through_the_transliteration(self):
        self.assertFalse(self.narrow("Veter Peremen", "Ветер сердца"))
        self.assertFalse(self.narrow("Veter Live", "Ветер Ливе"))          # an edition word survives the bridge
        self.assertFalse(self.narrow("Veter Vol 2", "Ветер Вол 3"))
        self.assertFalse(self.narrow("Veter Peremen", "Ветер другой перемен"))

    def test_the_lossy_table_never_bridges_two_latin_names(self):
        # w/v both map to one letter: through the table these would be equal, by name they are different albums
        self.assertFalse(self.narrow("Wolna Wolna", "Volna Volna"))
        self.assertFalse(self.narrow("Wolna Wolna", "Волна Волна Wolna"))  # a mixed-script name is not bridged either

    def test_cover_url_from_match_uses_the_narrow_rule_only_on_request(self):
        m = cand(album="Quuxnaut Album", artist="Zorblax")
        self.assertEqual(match.cover_url_from_match("Quuxnaut Album - Single", "Zorblax", m)[1], "album_mismatch")
        url, why = match.cover_url_from_match("Quuxnaut Album - Single", "Zorblax", m, narrow=True)
        self.assertEqual(why, "ok")
        self.assertTrue(url.endswith(match.COVER_SIZE_SUFFIX))

    def test_the_artist_check_still_applies_to_the_narrow_rule(self):
        m = cand(album="Quuxnaut Album", artist="Wibblewobble")
        self.assertEqual(match.cover_url_from_match("Quuxnaut Album - Single", "Zorblax", m, narrow=True),
                         (None, "artist_mismatch"))


class TracklistTest(NoNetwork):
    def run_level(self, tracks, **kw):
        t = todo(**kw)
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit()]}, {BID: page(*tracks)})
        return level(t, ytm), ytm

    def test_the_stored_track_in_the_tracklist_accepts_with_the_hit_thumbnail(self):
        r, ytm = self.run_level(["Other", "Frobnitz Song"])
        self.assertEqual((r.reason, r.browse_id, r.url), ("ok", BID, THUMB_URL))
        self.assertEqual(ytm.searches, [("Zorblax Quuxnaut Album", "albums", 5)])
        self.assertEqual(ytm.fetches, [BID])

    def test_a_tracklist_without_the_stored_track_is_rejected(self):
        r, ytm = self.run_level(["Other", "Another One"])
        self.assertEqual((r.url, r.browse_id, r.reason), (None, None, "tracklist"))
        self.assertEqual(ytm.fetches, [BID])  # it was looked at, and it failed

    def test_an_empty_or_missing_tracklist_is_rejected(self):
        for p in ({"tracks": []}, {}, {"tracks": None}, {"tracks": [None, {"title": None}, {"title": ""}]}, None):
            with self.subTest(page=p):
                ytm = StubYtm({"Zorblax Quuxnaut Album": [hit()]}, {BID: p})
                self.assertEqual(level(todo(), ytm).reason, "tracklist")

    def test_a_variant_of_the_stored_track_is_not_the_stored_track(self):
        for other in ("Frobnitz Song (Live)", "Frobnitz Song - Remix", "Frobnitz Song (Acoustic)"):
            with self.subTest(other=other):
                r, _ = self.run_level([other])
                self.assertEqual(r.reason, "tracklist")

    def test_the_title_bar_is_085(self):
        self.assertEqual(match.TITLE_MIN_SIMILARITY, 0.85)
        self.assertEqual(self.run_level(["abcdefghi"], title="abcdefghij")[0].reason, "ok")      # 0.947
        self.assertEqual(self.run_level(["abcdefg"], title="abcdefghij")[0].reason, "tracklist")  # 0.82

    def test_version_noise_in_the_tracklist_title_is_tolerated_like_the_matcher_does(self):
        self.assertEqual(self.run_level(["Frobnitz Song (Remastered 2011)"])[0].reason, "ok")
        self.assertEqual(self.run_level(["frobnitz song!"])[0].reason, "ok")

    def test_the_next_candidate_is_tried_when_the_first_fails_the_tracklist(self):
        t = todo()
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit(bid="MPREb_first001"), hit(bid="MPREb_second02")]},
                      {"MPREb_first001": page("Nope"), "MPREb_second02": page("Frobnitz Song")})
        r = level(t, ytm)
        self.assertEqual((r.reason, r.browse_id), ("ok", "MPREb_second02"))
        self.assertEqual(ytm.fetches, ["MPREb_first001", "MPREb_second02"])

    def test_the_number_of_album_pages_fetched_per_album_is_bounded(self):
        hits = [hit(bid=f"MPREb_cand{i:04d}") for i in range(5)]
        ytm = StubYtm({"Zorblax Quuxnaut Album": hits}, {})
        self.assertEqual(level(todo(), ytm).reason, "tracklist")
        self.assertEqual(len(ytm.fetches), bc.MAX_ALBUM_FETCHES)

    def test_the_thumbnail_falls_back_to_the_album_page_and_a_missing_one_rejects(self):
        base = "https://lh3.googleusercontent.com/PAGETHUMB"
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit(thumb=None)]}, {BID: page("Frobnitz Song", thumb=base + "=w60")})
        self.assertEqual(level(todo(), ytm).url, base + match.COVER_SIZE_SUFFIX)
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit(thumb=None)]}, {BID: page("Frobnitz Song")})
        self.assertEqual(level(todo(), ytm).reason, "no_thumbnail")
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit(thumb="http://insecure.example/x=w1")]}, {BID: page("Frobnitz Song")})
        self.assertEqual(level(todo(), ytm).reason, "no_thumbnail")


class AlbumLevelGuardsTest(NoNetwork):
    """The existing guards, through the album-level source: one case per bar, and the page is NOT fetched for a hit a guard
    rejected (so the tracklist check cannot rescue a hit the guards refused)."""

    def reject(self, hits, reason, **kw):
        t = todo(**kw)
        ytm = StubYtm({f"{t.artist} {t.album}": hits}, {BID: page("Frobnitz Song")})
        r = level(t, ytm)
        self.assertEqual((r.url, r.reason), (None, reason))
        self.assertEqual(ytm.fetches, [])

    def test_album_similarity_bar(self):
        self.reject([hit(title="abcdefgh")], "album_mismatch", album="abcdefghij")
        self.assertEqual(level(todo(album="abcdefghij"),
                               StubYtm({"Zorblax abcdefghij": [hit(title="abcdefghi")]}, {BID: page("Frobnitz Song")})).reason, "ok")

    def test_edition_words_plural_and_numbers(self):
        self.reject([hit(title="Quuxnaut Album (Deluxe Edition)")], "album_mismatch")
        self.reject([hit(title="Quuxnaut Album (Live)")], "album_mismatch")
        self.reject([hit(title="Quuxnaut Albums")], "album_mismatch")
        self.reject([hit(title="Quuxnaut Album Vol 2")], "album_mismatch", album="Quuxnaut Album Vol 1")
        self.reject([hit(title="Quuxnaut Album III")], "album_mismatch", album="Quuxnaut Album II")
        self.reject([hit(title="Quuxnaut Album Remastered")], "album_mismatch")

    def test_another_album_is_rejected(self):
        self.reject([hit(title="Wibblewobble Collection")], "album_mismatch")
        self.reject([], "no_hits")

    def test_a_namesake_album_by_another_artist_is_rejected(self):
        self.reject([hit(artists=("Wibblewobble",))], "artist_mismatch")

    def test_the_artist_bar_is_080_and_a_bare_plural_is_another_artist(self):
        self.assertEqual(match.COVER_ARTIST_MIN_SIMILARITY, 0.8)
        ytm = StubYtm({"abcdefghij Quuxnaut Album": [hit(artists=("abcdefgh",))]}, {BID: page("Frobnitz Song")})
        self.assertEqual(level(todo(artist="abcdefghij"), ytm).reason, "ok")                  # 0.889
        self.reject([hit(artists=("abcdef",))], "artist_mismatch", artist="abcdefghij")        # 0.75
        self.reject([hit(artists=("Zorblaxs",))], "artist_mismatch")

    def test_a_latin_artist_matches_its_cyrillic_spelling_and_a_wrong_cyrillic_name_does_not(self):
        t = todo(artist="Zorblax")
        ytm = StubYtm({"Zorblax Quuxnaut Album": [hit(artists=("Зорблакс",))]}, {BID: page("Frobnitz Song")})
        self.assertEqual(level(t, ytm).reason, "ok")
        self.reject([hit(artists=("Фробниц",))], "artist_mismatch")

    def test_the_pseudo_artist_denylist(self):
        t = todo(artist="Various Artists")
        ytm = StubYtm({"Various Artists Quuxnaut Album": [hit(artists=("Various Artists",))]}, {BID: page("Frobnitz Song")})
        self.assertEqual(level(t, ytm).reason, "pseudo_artist")
        self.assertEqual((ytm.searches, ytm.fetches), ([], []))  # refused before any call
        self.reject([hit(artists=("Zorblax", "Various Artists"))], "pseudo_artist")
        self.reject([hit(artists=("Zorblax", "Unknown"))], "pseudo_artist")

    def test_a_hostile_browse_id_is_never_fetched(self):
        for bid in (None, "", "x", "MPREb/../../x", "MPREb fake0001", "a" * 65, 7):
            with self.subTest(bid=bid):
                self.reject([hit(bid=bid)], "no_browse_id")

    def test_malformed_hits_are_skipped(self):
        self.reject([None, "x", {"title": None}, {"title": ["a"]}, {}], "no_hits")
        t = todo()
        for bad in (None, "x", {"a": 1}):
            ytm = StubYtm({"Zorblax Quuxnaut Album": bad}, {})
            self.assertEqual(level(t, ytm).reason, "no_hits")
        self.reject([hit(artists=())], "artist_mismatch")


class SecondSourceBase(Base):
    """Base of the workflow tests: the matcher answers per stored album name, the album-level source is the stub."""

    def setUp(self):
        super().setUp()
        self.ytm = StubYtm()

        class WithYtm(FakeBackends):
            def backends(inner):
                b = super().backends()
                b.ytm = self.ytm
                return b
        self.fb = WithYtm(self.d1)

    def album(self, album, title="Frobnitz Song", artist="Zorblax"):
        return self.d1.add(artist, album, title=title)

    def plan_path(self) -> Path:
        return self.dir / bc.PLAN_FILE

    def plan(self) -> dict:
        return json.loads(self.plan_path().read_text())

    def dry(self, *extra, **kw):
        return self.covers("--dry-run", *extra, **kw)

    def approve(self, approved=True, mode=0o600):
        doc = self.plan()
        doc["approved"] = approved
        self.plan_path().write_text(json.dumps(doc))
        os.chmod(self.plan_path(), mode)

    def apply(self, *extra, path=None, **kw):
        return self.covers("--apply-plan", str(path or self.plan_path()), *extra, **kw)

    def no_io(self):
        self.d1.write_hook = lambda s, p: (_ for _ in ()).throw(AssertionError("must not write D1"))
        self.fb.upload_error = AssertionError("must not send to Telegram")

    def mixed_library(self):
        """Four albums: strict accept, narrow accept, album-level accept, and one every source rejects."""
        ids = {"strict": self.album("Quuxnaut Strict"), "narrow": self.album("Quuxnaut Narrow - Single"),
               "second": self.album("Quuxnaut Second"), "none": self.album("Quuxnaut None")}
        self.fb.matches = {"Quuxnaut Narrow - Single": cand(album="Quuxnaut Narrow", artist="Zorblax"),
                           "Quuxnaut Second": cand(album="Wibblewobble Compilation", artist="Zorblax"),
                           "Quuxnaut None": None}
        self.ytm.hits = {"Zorblax Quuxnaut Second": [hit(title="Quuxnaut Second", bid="MPREb_second01")],
                         "Zorblax Quuxnaut None": [hit(title="Quuxnaut None", artists=("Wibblewobble",))]}
        self.ytm.pages = {"MPREb_second01": page("Frobnitz Song")}
        return ids


class DryRunTest(SecondSourceBase):
    def test_the_dry_run_prints_counts_per_rule_and_writes_the_plan(self):
        ids = self.mixed_library()
        self.assertEqual(self.dry(), 0)
        self.assertIn("would accept 3 (track 1, normalised 1, album_search 1)", self.out)
        self.assertIn("first source did not accept: album_mismatch 2, no_match 1", self.out)
        self.assertIn("rejected by every source (album-level reason): artist_mismatch 1", self.out)
        entries = self.plan()["entries"]
        self.assertEqual({k: v["rule"] for k, v in entries.items()},
                         {ids["strict"]: "track", ids["narrow"]: "normalised", ids["second"]: "album_search"})
        self.assertEqual(entries[ids["second"]]["browse_id"], "MPREb_second01")
        self.assertIsNone(entries[ids["strict"]]["browse_id"])
        self.assertTrue(all(e["cover_url"].endswith(match.COVER_SIZE_SUFFIX) for e in entries.values()))

    def test_the_dry_run_writes_nothing_to_d1_or_telegram(self):
        self.mixed_library()
        self.no_io()
        before = self.d1.db.execute("SELECT id, cover_file_id FROM albums ORDER BY id").fetchall()
        self.assertEqual(self.dry(), 0)
        self.assertEqual((self.d1.writes, self.fb.uploads), ([], []))
        self.assertEqual(self.d1.db.execute("SELECT id, cover_file_id FROM albums ORDER BY id").fetchall(), before)
        for sql, _params in self.d1.queries:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
        self.assertFalse((self.dir / bc.STATE_FILE).exists())  # not even the cooldown file
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([bootstrap.LOCK_FILE, bc.PLAN_FILE]))

    def test_the_plan_is_private_and_not_approved_and_carries_no_name(self):
        self.mixed_library()
        self.dry()
        st = self.plan_path().stat()
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        doc = self.plan()
        self.assertEqual((doc["version"], doc["approved"]), (1, False))
        text = self.plan_path().read_text() + self.out + self.err
        for name in SENTINELS + ("Quuxnaut", "Wibblewobble Compilation"):
            self.assertNotIn(name, text)

    def test_the_dry_run_rewrites_an_approved_plan_as_not_approved(self):
        self.mixed_library()
        self.dry()
        self.approve()
        self.assertTrue(self.plan()["approved"])
        self.dry()
        self.assertFalse(self.plan()["approved"])

    def test_the_dry_run_needs_the_caps_and_excludes_the_other_modes(self):
        self.mixed_library()
        for argv in (("covers", "--dry-run"), ("covers", "--dry-run", "--count"),
                     ("covers", "--dry-run", "--apply-plan", "x", "--max-new", "1", "--max-minutes", "1"),
                     ("covers", "--count", "--apply-plan", "x")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv), 2)
        self.assertEqual((self.constructed, self.d1.queries, self.fb.found), (0, [], []))

    def test_the_dry_run_takes_the_lock_and_has_the_guard(self):
        self.mixed_library()
        self.dir.mkdir(parents=True, mode=0o700)
        import fcntl
        fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        self.assertEqual(self.dry(), 75)
        self.assertEqual((self.constructed, self.d1.queries), (0, []))
        fcntl.flock(fd, fcntl.LOCK_UN)
        from unittest import mock
        with mock.patch.object(bootstrap, "local_now", lambda: datetime.datetime(2026, 10, 11, 21, 5)):
            self.assertEqual(self.dry(), 78)
        self.assertEqual((self.constructed, self.d1.queries), (0, []))

    def test_the_dry_run_honours_the_caps_and_the_cooldown(self):
        ids = self.mixed_library()
        self.assertEqual(self.dry(max_new="2"), 0)
        self.assertIn("not attempted 2", self.out)
        self.assertIn("stop reason cap", self.out)
        # a cooldown entry keeps an album out, --retry-days 0 puts it back
        state = bc.CoverState(self.dir / bc.STATE_FILE)
        for a in ids.values():
            state.record(a, "mismatch", datetime.date(2026, 10, 11))
        state.save()
        self.assertEqual(self.dry(), 0)
        self.assertIn("would accept 0", self.out)
        self.assertIn("cooldown 4", self.out)
        self.assertEqual(self.dry("--retry-days", "0"), 0)
        self.assertIn("would accept 3", self.out)
        self.assertEqual(len(bc.CoverState.load(self.dir / bc.STATE_FILE).albums), 4)  # untouched

    def test_a_sign_in_challenge_stops_the_dry_run_with_exit_3(self):
        self.mixed_library()
        self.fb.matches = {k: ingest.YouTubeSignInRequired("x") for k in ("Quuxnaut Strict", "Quuxnaut Narrow - Single",
                                                                           "Quuxnaut Second", "Quuxnaut None")}
        self.assertEqual(self.dry(), 3)
        self.assertEqual(len(self.fb.found), 1)  # never retried, nothing else attempted

    def test_a_failing_album_is_counted_and_three_in_a_row_stop_the_run(self):
        self.mixed_library()
        for k in ("Quuxnaut Strict", "Quuxnaut Narrow - Single", "Quuxnaut Second", "Quuxnaut None"):
            self.fb.matches[k] = RuntimeError("boom Zorblax")
        self.assertEqual(self.dry(), 1)
        self.assertIn("stop reason failed", self.out)
        self.assertNotIn("Zorblax", self.out + self.err)  # only the exception class is logged

    def test_the_album_level_source_runs_only_for_albums_the_first_source_did_not_accept(self):
        self.mixed_library()
        self.dry()
        self.assertEqual(sorted(q for q, _f, _l in self.ytm.searches), ["Zorblax Quuxnaut None", "Zorblax Quuxnaut Second"])

    def test_without_a_search_client_the_album_level_source_is_reported_unavailable(self):
        self.mixed_library()
        self.ytm = None
        self.assertEqual(self.dry(), 0)
        self.assertIn("album-level reason): unavailable 2", self.out)

    def test_the_album_level_rule_goes_through_the_guards_and_the_tracklist(self):
        a = self.album("Quuxnaut Second")
        self.fb.matches = {"Quuxnaut Second": cand(album="Wibblewobble Compilation", artist="Zorblax")}
        self.ytm.hits = {"Zorblax Quuxnaut Second": [hit(title="Quuxnaut Second", bid="MPREb_second01")]}
        self.ytm.pages = {"MPREb_second01": page("Not The Track")}
        self.dry()
        self.assertIn("would accept 0", self.out)
        self.assertIn("album-level reason): tracklist 1", self.out)
        self.assertEqual(self.plan()["entries"], {})
        self.assertNotIn(a, self.out)


class PlainRunUnchangedTest(SecondSourceBase):
    def test_the_real_run_without_a_plan_keeps_the_strict_guard(self):
        a = self.album("Quuxnaut Narrow - Single")
        self.fb.matches = {"Quuxnaut Narrow - Single": cand(album="Quuxnaut Narrow", artist="Zorblax")}
        self.assertEqual(self.covers(), 0)
        self.assertIsNone(self.d1.cover(a))
        self.assertEqual((self.fb.uploads, self.ytm.searches, self.ytm.fetches), ([], [], []))
        self.assertFalse((self.dir / bc.PLAN_FILE).exists())


class ApplyPlanTest(SecondSourceBase):
    def reviewed_plan(self):
        ids = self.mixed_library()
        self.assertEqual(self.dry(), 0)
        self.approve()
        self.fb.found.clear()
        self.ytm.searches.clear()
        self.ytm.fetches.clear()
        return ids

    def assert_nothing_happened(self):
        self.assertEqual((self.d1.writes, self.fb.uploads, self.fb.found, self.ytm.searches, self.constructed),
                         ([], [], [], [], 0))
        self.assertFalse((self.dir / bc.STATE_FILE).exists())

    def test_an_unapproved_plan_is_refused_before_anything_happens(self):
        ids = self.mixed_library()
        self.dry()
        self.fb.found.clear()
        self.d1.queries.clear()
        self.ytm.searches.clear()
        self.constructed = 0
        self.assertEqual(self.apply(), 2)
        self.assertIn("not approved", self.err)
        self.assert_nothing_happened()
        self.assertEqual(self.d1.queries, [])  # not even a D1 read
        for a in ids.values():
            self.assertIsNone(self.d1.cover(a))

    def test_the_approved_flag_must_be_the_json_value_true(self):
        self.mixed_library()
        self.dry()
        for value in (False, 1, "true", "yes", None, [True]):
            with self.subTest(value=value):
                doc = self.plan()
                doc["approved"] = value
                self.plan_path().write_text(json.dumps(doc))
                os.chmod(self.plan_path(), 0o600)
                self.assertEqual(self.apply(), 2)
        self.assertEqual((self.d1.writes, self.fb.uploads), ([], []))

    def test_an_unusable_plan_is_refused(self):
        self.mixed_library()
        self.dry()
        good = self.plan()
        cases = {"missing": None, "not json": "{", "list": "[]", "extra field": {**good, "x": 1},
                 "no entries": {k: v for k, v in good.items() if k != "entries"},
                 "bad version": {**good, "approved": True, "version": 2},
                 "bad id": {**good, "approved": True, "entries": {"nothex": next(iter(good["entries"].values()))}},
                 "bad rule": {**good, "approved": True, "entries": {next(iter(good["entries"])): {"rule": "x", "browse_id": None,
                                                                                                 "cover_url": THUMB_URL}}},
                 "http url": {**good, "approved": True, "entries": {next(iter(good["entries"])): {
                     "rule": "track", "browse_id": None, "cover_url": "http://x/y" + match.COVER_SIZE_SUFFIX}}},
                 "extra entry field": {**good, "approved": True, "entries": {next(iter(good["entries"])): {
                     "rule": "track", "browse_id": None, "cover_url": THUMB_URL, "name": "x"}}}}
        for label, doc in cases.items():
            with self.subTest(case=label):
                path = self.dir / "other.json"
                path.unlink(missing_ok=True)
                if doc is not None:
                    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
                    os.chmod(path, 0o600)
                self.assertEqual(self.apply(path=path), 2)
                self.assertIn("plan file unusable", self.err)
        self.assertEqual((self.d1.writes, self.fb.uploads), ([], []))

    def test_a_plan_that_others_can_write_is_refused(self):
        self.mixed_library()
        self.dry()
        for mode in (0o666, 0o660, 0o602):
            with self.subTest(mode=oct(mode)):
                self.approve(mode=mode)
                self.assertEqual(self.apply(), 2)
                self.assertIn("writable", self.err)
        self.assertEqual((self.d1.writes, self.fb.uploads), ([], []))

    def test_an_approved_plan_applies_exactly_the_planned_covers_without_any_search(self):
        ids = self.reviewed_plan()
        self.assertEqual(self.apply(), 0)
        self.assertEqual((self.fb.found, self.ytm.searches, self.ytm.fetches), ([], [], []))  # no new decision, no YouTube
        urls = sorted(s["cover_url"] for s in self.fb.uploads)
        self.assertEqual(urls, sorted(e["cover_url"] for e in self.plan()["entries"].values()))
        self.assertTrue(all(s[ingest.COVER_FROM_MATCH] for s in self.fb.uploads))  # the host allowlist route
        for k in ("strict", "narrow", "second"):
            self.assertIsNotNone(self.d1.cover(ids[k]))
        self.assertIsNone(self.d1.cover(ids["none"]))  # not in the plan: left alone
        self.assertEqual([w[0] for w in self.d1.writes], [bc.UPDATE_SQL] * 3)
        self.assertTrue(all(len(w[1]) == 2 for w in self.d1.writes))

    def test_a_stored_cover_always_wins(self):
        ids = self.reviewed_plan()
        self.d1.db.execute("UPDATE albums SET cover_file_id = 'owner-cover' WHERE id = ?", [ids["narrow"]])
        self.d1.db.commit()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.d1.cover(ids["narrow"]), "owner-cover")
        self.assertEqual(len(self.fb.uploads), 2)  # the album with a cover is not even selected
        # a cover stored between the read and the write is not replaced either
        self.d1.db.execute("UPDATE albums SET cover_file_id = NULL WHERE id = ?", [ids["narrow"]])
        self.d1.db.execute("UPDATE albums SET cover_file_id = NULL WHERE id = ?", [ids["strict"]])
        self.d1.db.commit()
        self.d1.write_hook = lambda s, p: self.d1.db.execute("UPDATE albums SET cover_file_id = 'late' WHERE id = ?", [p[1]])
        self.assertEqual(self.apply(), 0)
        self.assertEqual({self.d1.cover(ids["narrow"]), self.d1.cover(ids["strict"])}, {"late"})

    def test_the_cooldown_the_caps_the_lock_and_the_guard_still_apply(self):
        ids = self.reviewed_plan()
        state = bc.CoverState(self.dir / bc.STATE_FILE)
        state.record(ids["strict"], "mismatch", datetime.date(2026, 10, 11))
        state.save()
        self.assertEqual(self.apply(max_new="1"), 0)
        self.assertIn("stop reason: cap", self.out)
        self.assertEqual(len(self.fb.uploads), 1)
        self.assertIsNone(self.d1.cover(ids["strict"]))           # in cooldown
        self.assertEqual(self.apply(), 0)
        self.assertIsNone(self.d1.cover(ids["strict"]))           # still resting
        self.assertEqual(self.apply("--retry-days", "0"), 0)
        self.assertIsNotNone(self.d1.cover(ids["strict"]))
        self.assertEqual(self.apply(max_new="0"), 2)               # the caps are range checked
        self.assertEqual(self.main("covers", "--apply-plan", str(self.plan_path())), 2)  # and required

    def test_the_guard_and_the_lock_refuse_before_the_plan_is_applied(self):
        ids = self.reviewed_plan()
        from unittest import mock
        with mock.patch.object(bootstrap, "local_now", lambda: datetime.datetime(2026, 10, 11, 21, 5)):
            self.assertEqual(self.apply(), 78)
        self.assertEqual(self.fb.uploads, [])
        import fcntl
        fd = os.open(self.dir / bootstrap.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        self.assertEqual(self.apply(), 75)
        self.assertEqual((self.fb.uploads, self.d1.writes), ([], []))
        for a in ids.values():
            self.assertIsNone(self.d1.cover(a))

    def test_a_failed_upload_is_a_failure_that_rests_and_stores_nothing(self):
        ids = self.reviewed_plan()
        self.fb.upload_error = ingest.IngestError("cover failed")
        self.assertEqual(self.apply(), 1)
        for a in ids.values():
            self.assertIsNone(self.d1.cover(a))
        self.assertEqual({e["result"] for e in bc.CoverState.load(self.dir / bc.STATE_FILE).albums.values()}, {"failed"})

    def test_the_apply_logs_hold_counts_only(self):
        self.reviewed_plan()
        self.apply()
        text = self.out + self.err
        for name in SENTINELS + ("Quuxnaut",):
            self.assertNotIn(name, text)


class PacedAlbumCallTest(NoNetwork):
    def test_get_album_is_paced_retried_and_stops_on_the_sign_in_text(self):
        class Ytm:
            def __init__(self, errors):
                self.errors, self.calls = list(errors), 0

            def get_album(self, bid):
                self.calls += 1
                if self.errors:
                    raise self.errors.pop(0)
                return {"tracks": []}
        clock = [0.0]
        sleeps: list = []

        def sleep(s):
            sleeps.append(s)
            clock[0] += s
        ytm = Ytm([])
        paced = bc.PacedSearch(ytm, ingest.Throttle(0, 0, 3, sleep=sleep, clock=lambda: clock[0]), sleep=sleep,
                               clock=lambda: clock[0], rng=lambda: 0.0)
        paced.get_album(BID)
        paced.get_album(BID)
        self.assertEqual((ytm.calls, paced.calls, sleeps), (2, 2, [bc.PACE_SECONDS]))
        ytm = Ytm([OSError("net")])  # a transient error is retried, a bounded number of times
        paced = bc.PacedSearch(ytm, ingest.Throttle(0, 0, 3, sleep=sleep, clock=lambda: clock[0]), sleep=sleep,
                               clock=lambda: clock[0], rng=lambda: 0.0)
        paced.get_album(BID)
        self.assertEqual(ytm.calls, 2)
        ytm = Ytm([RuntimeError("Sign in to confirm you're not a bot")])
        paced = bc.PacedSearch(ytm, ingest.Throttle(0, 0, 3, sleep=sleep, clock=lambda: clock[0]), sleep=sleep,
                               clock=lambda: clock[0], rng=lambda: 0.0)
        with self.assertRaises(ingest.YouTubeSignInRequired):
            paced.get_album(BID)
        self.assertEqual(ytm.calls, 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for lastfm_tracks: a fake Last.fm behind an injected fetch. No real network, no sleeping, no real key."""
import re
import unittest
import urllib.parse

import lastfm as lf
import lastfm_tracks as lt

KEY = "canaryKey0123456789abcdefABCDEF01"
MBID = "11111111-2222-3333-4444-555555555555"
ARTIST = "Fake Artist"
ID_RE = re.compile(r"lfm:[0-9a-f]{16}")


def tr(name, dur="200", rank=None, artist=None):
    t = {"name": name, "duration": dur, "url": "https://example.invalid/t"}
    if rank is not None:
        t["@attr"] = {"rank": str(rank)}
    if artist is not None:
        t["artist"] = {"name": artist, "mbid": "", "url": "https://example.invalid/a"}
    return t


def album_info(name, tracks):
    return {"album": {"name": name, "artist": ARTIST, "mbid": "", "tracks": {"track": tracks}}}


def top_albums(names, page=1, pages=1, mbids=None):
    mbids = mbids or {}
    return {"topalbums": {"album": [{"name": n, "mbid": mbids.get(n, ""), "artist": {"name": ARTIST}} for n in names],
                          "@attr": {"artist": ARTIST, "page": str(page), "perPage": "50", "totalPages": str(pages)}}}


def error(code, msg="x"):
    return 200, {"error": code, "message": msg}


class Fake:
    """fetch(url, timeout). `pages` is the list of getTopAlbums bodies by page; `infos` maps an album name (or mbid)
    to an album.getInfo body, or to a (status, body) tuple. Records the parsed queries."""

    def __init__(self, pages, infos, top_error=None):
        self.pages, self.infos, self.top_error, self.queries = pages, infos, top_error, []

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["api_key"] == KEY and q["format"] == "json"
        self.queries.append(q)
        if q["method"] == "artist.getTopAlbums":
            if self.top_error:
                return self.top_error
            return 200, self.pages[int(q["page"]) - 1]
        got = self.infos[q.get("mbid") or q["album"]]
        return got if isinstance(got, tuple) else (200, got)


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run(fake, artist=ARTIST, mbid=None, **kw):
    clk = Clock()
    client = lf.LastfmClient(KEY, fetch=fake, sleep=clk.sleep, clock=clk.now, out=lambda s: None)
    logged = []
    res = lt.artist_tracks(client, artist, mbid, out=logged.append, **kw)
    return res, client, clk, logged


class EndToEnd(unittest.TestCase):
    def fake(self):
        return Fake([top_albums(["Alpha", "Beta"], mbids={"Beta": MBID})], {
            "Alpha": album_info("Alpha", [tr("One", "201", 1), tr("Two", "0", 2), tr("Three", None, 3)]),
            MBID: album_info("Beta", [tr("One", "199", 1), tr("Four", "180", 2, artist="Guest Singer")])})

    def test_records_shape_and_queries(self):
        f = self.fake()
        res, client, clk, logged = run(f)
        self.assertEqual(res.status, lt.OK)
        self.assertEqual([r["name"] for r in res.records], ["One", "Two", "Three", "Four"])
        one = res.records[0]
        self.assertRegex(one["song_id"], ID_RE)
        self.assertEqual({k: v for k, v in one.items() if k != "song_id"}, {
            "name": "One", "artists": [ARTIST], "album_artist": ARTIST, "album_name": "Alpha", "year": None,
            "track_number": 1, "disc_number": 1, "duration": 201, "url": None, "cover_url": None,
            "album_type": None, "release_date": None, "explicit": None})
        self.assertEqual([r["duration"] for r in res.records], [201, None, None, 180])
        self.assertEqual(res.records[3]["artists"], [ARTIST, "Guest Singer"])  # a featured artist Last.fm gives
        self.assertEqual(res.records[3]["album_name"], "Beta")
        self.assertEqual((res.tracks, res.duplicates, res.dropped, res.no_duration, res.albums), (5, 1, 0, 2, 2))
        methods = [(q["method"], q.get("mbid"), q.get("album")) for q in f.queries]
        self.assertEqual(methods, [("artist.getTopAlbums", None, None), ("album.getInfo", None, "Alpha"),
                                   ("album.getInfo", MBID, None)])
        self.assertEqual(f.queries[0]["limit"], "50")
        self.assertEqual(f.queries[0]["autocorrect"], "1")
        self.assertEqual((f.queries[1]["artist"], f.queries[1]["autocorrect"]), (ARTIST, "1"))  # no mbid: artist + album
        self.assertNotIn("artist", f.queries[2])  # by mbid
        self.assertEqual((client.calls, res.calls), (3, 3))
        self.assertTrue(all(s >= 1.0 for s in clk.sleeps))  # paced by the client: at least 1 s between calls
        self.assertEqual(len(clk.sleeps), 2)

    def test_log_line_has_counts_only(self):
        _, _, _, logged = run(self.fake())
        self.assertEqual(len(logged), 1)
        for word in ("One", "Alpha", ARTIST, "lfm:", KEY):
            self.assertNotIn(word, logged[0])
        self.assertIn("1 duplicates", logged[0])

    def test_artist_mbid_is_used_for_the_albums_call(self):
        f = self.fake()
        run(f, mbid=MBID.upper())
        self.assertEqual(f.queries[0]["mbid"], MBID)
        self.assertNotIn("artist", f.queries[0])


class Paging(unittest.TestCase):
    def test_pages_are_followed_to_the_end(self):
        a = [f"A{i}" for i in range(50)]
        b = [f"B{i}" for i in range(7)]
        infos = {n: album_info(n, [tr("T " + n)]) for n in a + b}
        f = Fake([top_albums(a, 1, 2), top_albums(b, 2, 2)], infos)
        res, client, _, _ = run(f)
        self.assertEqual((len(res.records), res.albums, client.calls), (57, 57, 2 + 57))
        self.assertEqual([q["page"] for q in f.queries if q["method"] == "artist.getTopAlbums"], ["1", "2"])

    def test_max_albums_stops_reading_and_skips_the_extra_page(self):
        a = [f"A{i}" for i in range(50)]
        infos = {n: album_info(n, [tr("T " + n)]) for n in a}
        f = Fake([top_albums(a, 1, 9)], infos)
        res, client, _, _ = run(f, max_albums=10)
        self.assertEqual((res.albums, res.albums_truncated, client.calls), (10, True, 11))

    def test_default_bounds(self):
        self.assertEqual((lt.MAX_ALBUMS, lt.CALL_BUDGET, lt.PAGE_LIMIT), (100, 150, 50))
        a, b, c = ([f"{p}{i}" for i in range(50)] for p in "abc")
        infos = {n: album_info(n, [tr("T " + n)]) for n in a + b + c}
        f = Fake([top_albums(a, 1, 3), top_albums(b, 2, 3), top_albums(c, 3, 3)], infos)
        res, client, _, _ = run(f)
        self.assertEqual((res.albums, res.albums_truncated, client.calls), (100, True, 102))

    def test_empty_page_ends_paging(self):
        f = Fake([top_albums(["A"], 1, 500), top_albums([], 2, 500)], {"A": album_info("A", [tr("T")])})
        res, client, _, _ = run(f)
        self.assertEqual((res.albums, client.calls), (1, 3))

    def test_call_budget_stops_the_artist_with_a_clear_error(self):
        names = [f"A{i}" for i in range(50)]
        f = Fake([top_albums(names, 1, 1)], {n: album_info(n, [tr("T " + n)]) for n in names})
        with self.assertRaises(lt.ArtistBudgetExceeded) as cm:
            run(f, call_budget=20)
        self.assertIn("20", str(cm.exception))
        self.assertEqual(len(f.queries), 20)  # never one call over

    def test_budget_counts_from_the_artist_start_and_retries(self):
        names = ["A", "B"]
        f = Fake([top_albums(names)], {n: album_info(n, [tr("T " + n)]) for n in names})
        clk = Clock()
        client = lf.LastfmClient(KEY, fetch=f, sleep=clk.sleep, clock=clk.now, out=lambda s: None)
        for _ in range(3):  # three earlier artists on the same client do not eat this artist's budget
            lt.artist_tracks(client, ARTIST, out=lambda s: None, call_budget=3)
        self.assertEqual(client.calls, 9)

    def test_invalid_bounds_rejected(self):
        f = Fake([top_albums([])], {})
        for kw in ({"max_albums": 0}, {"call_budget": 0}, {"max_albums": True}, {"call_budget": "5"}):
            with self.assertRaises(lf.LastfmError):
                run(f, **kw)
        self.assertEqual(f.queries, [])


class Durations(unittest.TestCase):
    def dur(self, value):
        rec = lt.make_record(tr("X", value), ARTIST, "Alb")
        return rec["duration"]

    def test_unknown_is_none_never_zero(self):
        for v in ("0", 0, None, "", "abc", "-5", "1.5", "12e3", True, [], {}, "٣٤٥", str(lt.MAX_DURATION_S + 1)):
            self.assertIsNone(self.dur(v), repr(v))

    def test_known_is_whole_seconds(self):
        self.assertEqual((self.dur("245"), self.dur(245), self.dur("1"), self.dur(str(lt.MAX_DURATION_S))),
                         (245, 245, 1, lt.MAX_DURATION_S))

    def test_missing_duration_key(self):
        rec = lt.make_record({"name": "X"}, ARTIST, None)
        self.assertIsNone(rec["duration"])


class Collapse(unittest.TestCase):
    def test_same_title_on_two_albums_first_album_wins(self):
        f = Fake([top_albums(["Hits", "Original"])], {
            "Hits": album_info("Hits", [tr("Song", "0", 5)]),
            "Original": album_info("Original", [tr("Song", "210", 1)])})
        res, _, _, _ = run(f)
        self.assertEqual(len(res.records), 1)
        self.assertEqual((res.records[0]["album_name"], res.records[0]["track_number"], res.duplicates), ("Hits", 5, 1))
        self.assertIsNone(res.records[0]["duration"])  # the first one is kept as it is

    def test_same_title_twice_on_one_album(self):
        f = Fake([top_albums(["A"])], {"A": album_info("A", [tr("S"), tr("s!")])})
        res, _, _, _ = run(f)
        self.assertEqual((len(res.records), res.duplicates), (1, 1))

    def test_variants_stay_distinct(self):
        f = Fake([top_albums(["A"])], {"A": album_info("A", [tr("Song"), tr("Song - Live"), tr("Song (Remix)"),
                                                            tr("Song (Acoustic)")])})
        res, _, _, _ = run(f)
        self.assertEqual(len({r["song_id"] for r in res.records}), 4)

    def test_unusable_tracks_dropped_and_counted(self):
        bad = [tr(""), tr("   "), tr("???"), {"name": 5}, {"duration": "3"}, "x", None, tr("x" * 300), tr("Fine")]
        f = Fake([top_albums(["A"])], {"A": album_info("A", bad)})
        res, _, _, _ = run(f)
        self.assertEqual(([r["name"] for r in res.records], res.dropped, res.tracks), (["Fine"], 8, 9))

    def test_single_track_album_is_an_object_not_a_list(self):
        f = Fake([top_albums(["A"])], {"A": {"album": {"name": "A", "tracks": {"track": tr("Solo", "90", 1)}}}})
        res, _, _, _ = run(f)
        self.assertEqual([r["name"] for r in res.records], ["Solo"])

    def test_album_without_tracks(self):
        for tracks in (None, "", {}, {"track": []}):
            body = {"album": {"name": "A"}} if tracks is None else {"album": {"name": "A", "tracks": tracks}}
            res, _, _, _ = run(Fake([top_albums(["A"])], {"A": body}))
            self.assertEqual((res.records, res.albums, res.status), ([], 1, lt.OK), repr(tracks))

    def test_repeated_album_entries_are_read_once(self):
        f = Fake([top_albums(["A", "a", "", "B"], mbids={"B": MBID})], {"A": album_info("A", [tr("T")]),
                                                                       MBID: album_info("B", [tr("U")])})
        res, client, _, _ = run(f)
        self.assertEqual((res.albums, res.albums_dropped, client.calls), (2, 2, 3))

    def test_track_artist_equal_to_the_artist_is_not_a_feature(self):
        rec = lt.make_record(tr("T", artist=ARTIST.upper() + "!"), ARTIST, "A")
        self.assertEqual(rec["artists"], [ARTIST])

    def test_rank_bounds(self):
        for rank, want in ((1, 1), ("12", 12), (0, None), ("x", None), (10 ** 6, None), (None, None)):
            t = tr("T")
            if rank is not None:
                t["@attr"] = {"rank": rank}
            self.assertEqual(lt.make_record(t, ARTIST, "A")["track_number"], want, rank)


class Statuses(unittest.TestCase):
    def test_unknown_artist_is_an_empty_result_not_an_exception(self):
        f = Fake([], {}, top_error=error(6, "The artist you supplied could not be found"))
        res, _, _, logged = run(f)
        self.assertEqual((res.records, res.status), ([], lt.UNKNOWN_ARTIST))
        self.assertEqual(len(logged), 1)

    def test_artist_without_albums(self):
        for body in (top_albums([]), {"topalbums": {"album": [], "@attr": {"totalPages": "0"}}},
                     {"topalbums": {"@attr": {"totalPages": "0"}}}):
            res, client, _, _ = run(Fake([body], {}))
            self.assertEqual((res.records, res.status, client.calls), ([], lt.NO_ALBUMS, 1))

    def test_unusable_artist_name_makes_no_call(self):
        f = Fake([], {})
        res, client, _, _ = run(f, artist="!!!")
        self.assertEqual((res.status, res.records, client.calls), (lt.UNUSABLE_ARTIST, [], 0))

    def test_album_not_found_is_skipped(self):
        f = Fake([top_albums(["Gone", "Here"])], {"Gone": error(6, "Album not found"),
                                                  "Here": album_info("Here", [tr("T")])})
        res, _, _, _ = run(f)
        self.assertEqual((len(res.records), res.albums, res.albums_not_found), (1, 1, 1))

    def test_other_api_errors_propagate(self):
        for code, text in ((10, "invalid API key"), (26, "suspended"), (17, "login required")):
            f = Fake([top_albums(["A"])], {"A": error(code)})
            with self.assertRaises(lf.LastfmError) as cm:
                run(f)
            self.assertIn(text, str(cm.exception))
        with self.assertRaises(lf.LastfmError):
            run(Fake([], {}, top_error=error(10)))

    def test_error_code_6_on_the_albums_call_only_for_that_error(self):
        e = lf.LastfmError("x")
        self.assertIsNone(e.code)


class Malformed(unittest.TestCase):
    def test_bad_shapes_raise_a_clear_error(self):
        for body in ({}, {"topalbums": []}, {"topalbums": {"album": "x"}}, {"topalbums": {"album": 5}}):
            with self.assertRaises(lf.LastfmError):
                run(Fake([body], {}))
        with self.assertRaises(lf.LastfmError):
            run(Fake([top_albums(["A"])], {"A": {"album": "x"}}))
        with self.assertRaises(lf.LastfmError):
            run(Fake([top_albums(["A"])], {"A": {"album": {"tracks": {"track": "x"}}}}))
        with self.assertRaises(lf.LastfmError):
            run(Fake([top_albums(["A"])], {"A": {"album": {"tracks": 5}}}))

    def test_bad_total_pages_is_treated_as_one_page(self):
        for pages in ("x", "-3", None, [], True):
            body = top_albums(["A"])
            body["topalbums"]["@attr"]["totalPages"] = pages
            res, client, _, _ = run(Fake([body], {"A": album_info("A", [tr("T")])}))
            self.assertEqual((res.albums, client.calls), (1, 2), repr(pages))

    def test_invalid_input(self):
        f = Fake([], {})
        for kw in ({"artist": ""}, {"artist": "  "}, {"artist": "x" * 300}, {"artist": None}, {"artist": 5},
                   {"mbid": "nope"}, {"mbid": 5}):
            with self.assertRaises(lf.LastfmError, msg=str(kw)):
                run(f, **kw)
        self.assertEqual(f.queries, [])

    def test_control_characters_are_stripped_from_the_record(self):
        f = Fake([top_albums(["Al\x1b[31mb\x00um\n"])], {"Al\x1b[31mb\x00um": album_info(
            "Al\x1b[31mb\x07um", [tr("Ti\x1b[2Jtle\r\n"), tr("Sp‮oof")])})
        f.infos = {k: f.infos["Al\x1b[31mb\x00um"] for k in ("Al[31mbum", "Al\x1b[31mb\x00um")}
        res, _, _, _ = run(f)
        for rec in res.records:
            for v in [rec["name"], rec["album_name"], *rec["artists"]]:
                self.assertTrue(all(c.isprintable() for c in v), repr(v))

    def test_hostile_values_cannot_inject_into_the_record(self):
        rec = lt.make_record({"name": "T", "artist": {"name": "A" * 300}, "duration": {"x": 1}}, ARTIST, "Al")
        self.assertEqual((rec["artists"], rec["duration"]), ([ARTIST], None))

    def test_deeply_nested_json_is_an_error_not_a_recursion_error(self):
        deep = b"[" * 100000 + b"]" * 100000
        self.assertEqual(lf._parse(deep), {})
        f = Fake([], {}, top_error=(200, lf._parse(deep)))
        with self.assertRaises(lf.LastfmError):
            run(f)


class Transport(unittest.TestCase):
    def client(self, fetch, **kw):
        clk = Clock()
        return lf.LastfmClient(KEY, fetch=fetch, sleep=clk.sleep, clock=clk.now, out=lambda s: None, **kw), clk

    def test_oversize_is_not_retried(self):
        n = []

        def fetch(url, timeout):
            n.append(url)
            raise lf.ResponseTooLarge("big")

        c, _ = self.client(fetch)
        with self.assertRaises(lf.LastfmError):
            lt.artist_tracks(c, ARTIST, out=lambda s: None)
        self.assertEqual(len(n), 1)

    def test_error_text_never_holds_the_key_or_a_url(self):
        def fetch(url, timeout):
            return 200, {"error": 99, "message": f"boom {KEY} https://x/?api_key={KEY}"}

        c, _ = self.client(fetch)
        with self.assertRaises(lf.LastfmError) as cm:
            lt.artist_tracks(c, ARTIST, out=lambda s: None)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("***", str(cm.exception))

    def test_retryable_error_counts_against_the_artist_budget(self):
        def fetch(url, timeout):
            return error(29)

        c, _ = self.client(fetch)
        with self.assertRaises(lt.ArtistBudgetExceeded):
            lt.artist_tracks(c, ARTIST, out=lambda s: None, call_budget=2)
        self.assertEqual(c.calls, 2)

    def test_only_read_methods_are_allowed(self):
        c, _ = self.client(lambda u, t: (200, {}))
        for m in ("album.addTags", "artist.getTopAlbums ", "track.scrobble"):
            with self.assertRaises(lf.LastfmError):
                c.call(m, artist="x")
        self.assertEqual(c.calls, 0)

    def test_key_only_in_the_query_of_the_request(self):
        f = Fake([top_albums(["A"])], {"A": album_info("A", [tr("T")])})
        seen = []
        orig = f.__call__

        def spy(url, timeout):
            seen.append(url)
            return orig(url, timeout)

        run(spy)
        self.assertTrue(all(u.startswith(lf.API + "?") for u in seen))
        self.assertTrue(all(u.count(KEY) == 1 and "api_key=" + KEY in u for u in seen))


class SongIdTests(unittest.TestCase):
    def test_shape(self):
        for a, t in ((ARTIST, "Song"), ("Ёлка", "Песня"), ("A" * 200, "T" * 200), ("Beyoncé", "Halo")):
            self.assertRegex(lt.song_id(a, t), r"lfm:[0-9a-f]{16}")
            self.assertTrue(re.fullmatch(r"lfm:[0-9a-f]{16}", lt.song_id(a, t)))

    def test_pinned_value(self):  # sha256("fake artist\x1fsong")[:16]: a change of the contract must break this test
        import hashlib
        want = "lfm:" + hashlib.sha256(b"fake artist\x1fsong").hexdigest()[:16]
        self.assertEqual(lt.song_id("Fake Artist", "Song"), want)

    def test_stable_across_calls(self):
        self.assertEqual(lt.song_id(ARTIST, "Song"), lt.song_id(ARTIST, "Song"))
        f = Fake([top_albums(["A"])], {"A": album_info("A", [tr("Song")])})
        r1, _, _, _ = run(f)
        r2, _, _, _ = run(f)
        self.assertEqual(r1.records[0]["song_id"], r2.records[0]["song_id"])

    def test_case_and_punctuation_insensitive(self):
        base = lt.song_id("The Band", "Don't Stop")
        for a, t in (("the band", "don't stop"), ("THE BAND", "DON'T STOP"), ("The  Band!", "Don't   Stop?"),
                     ("The-Band", "Don’t Stop"), ("Ｔhe Band", "Don't Stop")):
            self.assertEqual(lt.song_id(a, t), base, (a, t))

    def test_different_artist_or_title_gives_a_different_id(self):
        ids = {lt.song_id("A", "Song"), lt.song_id("B", "Song"), lt.song_id("A", "Song 2"), lt.song_id("A", "Song - Live"),
               lt.song_id("A", "Song (Remix)")}
        self.assertEqual(len(ids), 5)

    def test_separator_prevents_ambiguity(self):
        self.assertNotEqual(lt.song_id("ab", "c"), lt.song_id("a", "bc"))

    def test_nothing_left_after_normalisation(self):
        self.assertIsNone(lt.song_id("!!!", "Song"))
        self.assertIsNone(lt.song_id("A", "???"))
        self.assertIsNone(lt.song_id("", ""))

    def test_album_does_not_change_the_id(self):
        f = Fake([top_albums(["X", "Y"])], {"X": album_info("X", [tr("Song")]), "Y": album_info("Y", [tr("Song")])})
        res, _, _, _ = run(f)
        self.assertEqual(len(res.records), 1)
        self.assertEqual(res.records[0]["song_id"], lt.song_id(ARTIST, "Song"))

    def test_the_id_is_accepted_by_the_ingest_check_when_present(self):
        import ingest
        if hasattr(ingest, "SONG_ID"):
            self.assertTrue(ingest.SONG_ID.fullmatch(lt.song_id(ARTIST, "Song")))


if __name__ == "__main__":
    unittest.main()

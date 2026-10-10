"""Tests for artist_tracks: a fake Spotify over an in-memory catalog, no network, no sleeping."""
import contextlib
import io
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
from unittest import mock

import artist_tracks as at
import ingest

ART = "A" * 22
OTHER = "B" * 22
CANARY = "canaryToken-0123456789abcdef"
SECRET = "canarySecret-fedcba9876543210"


def sid(prefix: str, n: int) -> str:
    return (prefix + str(n)).ljust(22, "x")[:22]


def track(n, artists=((ART, "Art"),), **kw):
    t = {"id": sid("T", n), "name": f"Song {n}", "duration_ms": 200500, "track_number": n, "disc_number": 1,
         "is_local": False, "artists": [{"id": i, "name": nm} for i, nm in artists]}
    t.update(kw)
    return t


def album(n, artists=((ART, "Art"),), **kw):
    a = {"id": sid("L", n), "name": f"Album {n}", "release_date": "2019-05-03", "album_type": "album",
         "artists": [{"id": i, "name": nm} for i, nm in artists], "images": [{"url": f"https://i.scdn.co/{n}"}]}
    a.update(kw)
    return a


class FakeSpotify:
    """albums: list of album dicts; tracks: {album id: [track dicts]}. Pages by the limit/offset in the URL and
    emits a `next` link like the real API. `script` maps a call number (1-based) to a canned (status, headers, body)."""

    def __init__(self, albums, tracks, script=None):
        self.albums, self.tracks, self.script, self.urls, self.auth = albums, tracks, script or {}, [], []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        self.auth.append(headers.get("Authorization"))
        assert timeout <= 15
        if len(self.urls) in self.script:
            return self.script[len(self.urls)]
        p = urllib.parse.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
        limit, off = int(q["limit"]), int(q["offset"])
        if p.path == f"/v1/artists/{ART}/albums":
            items, cap = self.albums, 10
        else:
            items, cap = self.tracks.get(p.path.split("/")[3], []), 50
        assert 1 <= limit <= cap, "limit above the documented maximum"
        page = items[off:off + limit]
        nxt = None
        if off + limit < len(items):
            nq = dict(q, offset=off + limit)
            nxt = f"https://api.spotify.com{p.path}?{urllib.parse.urlencode(nq)}"
        return 200, {}, {"items": page, "next": nxt, "total": len(items)}


def make(fake, **kw):
    sleeps = []
    cat = at.Catalog(lambda: CANARY, [SECRET], fetch=fake, sleep=sleeps.append, min_interval=0, out=lambda s: None, **kw)
    cat.sleeps = sleeps
    return cat


def run(fake, **kw):
    cat = make(fake)
    return cat, at.artist_tracks(cat, ART, out=lambda s: None, **kw)


class Validation(unittest.TestCase):
    def test_accepts_id_and_url(self):
        self.assertEqual(at.parse_artist(ART), ART)
        self.assertEqual(at.parse_artist(f"https://open.spotify.com/artist/{ART}"), ART)
        self.assertEqual(at.parse_artist(f"https://open.spotify.com/artist/{ART}?si=abc123_-"), ART)

    def test_rejects_everything_else(self):
        for bad in ["", "short", ART + "x", ART[:-1] + "/", f"http://open.spotify.com/artist/{ART}",
                    f"https://open.spotify.com.evil.io/artist/{ART}", f"https://evil.io/https://open.spotify.com/artist/{ART}",
                    f"https://open.spotify.com/playlist/{ART}", f"https://open.spotify.com/artist/{ART}?x=1\n",
                    f"https://open.spotify.com/artist/{ART}/../x", f"{ART}\n", "../" + ART,
                    f"https://open.spotify.com/artist/{ART}#frag", "spotify:artist:" + ART]:
            with self.assertRaises(at.ArtistTracksError, msg=repr(bad)):
                at.parse_artist(bad)

    def test_groups(self):
        self.assertEqual(at.parse_groups("album,single"), ("album", "single"))
        self.assertEqual(at.parse_groups("album,album,appears_on"), ("album", "appears_on"))
        for bad in ["", "album,", "albums", "album,all", "album;single", "ALBUM", "album single"]:
            with self.assertRaises(at.ArtistTracksError, msg=bad):
                at.parse_groups(bad)

    def test_market(self):
        self.assertEqual(at.parse_market("DE"), "DE")
        for bad in ["", "de", "USA", "U", "U1", "US,DE", "US\n"]:
            with self.assertRaises(at.ArtistTracksError, msg=repr(bad)):
                at.parse_market(bad)

    def test_bad_input_makes_no_call(self):
        fake = FakeSpotify([], {})
        with self.assertRaises(at.ArtistTracksError):
            at.artist_tracks(make(fake), "nope", out=lambda s: None)
        self.assertEqual(fake.urls, [])


class Enumerate(unittest.TestCase):
    def test_request_parameters(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1)]})
        run(fake)
        q0 = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
        self.assertEqual(q0, {"include_groups": ["album,single"], "limit": ["10"], "offset": ["0"], "market": ["US"]})
        q1 = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[1]).query)
        self.assertEqual(q1, {"limit": ["50"], "offset": ["0"], "market": ["US"]})
        self.assertEqual(fake.auth, [f"Bearer {CANARY}"] * 2)

    def test_market_and_groups_are_passed(self):
        fake = FakeSpotify([], {})
        run(fake, market="DE", groups=("album", "single", "compilation", "appears_on"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.urls[0]).query)
        self.assertEqual(q["market"], ["DE"])
        self.assertEqual(q["include_groups"], ["album,single,compilation,appears_on"])

    def test_record_shape_matches_the_pipeline(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1, artists=((ART, "Art"), (OTHER, "Guest")))]})
        _, songs = run(fake)
        self.assertEqual(songs, [{
            "song_id": sid("T", 1), "name": "Song 1", "artists": ["Art", "Guest"], "album_artist": "Art",
            "album_name": "Album 1", "year": 2019, "track_number": 1, "disc_number": 1, "duration": 200.5,
            "url": f"https://open.spotify.com/track/{sid('T', 1)}", "cover_url": "https://i.scdn.co/1",
            "album_type": "album", "release_date": "2019-05-03", "explicit": None}])
        self.assertEqual(ingest.primary_artist(songs[0]), "Art")
        self.assertTrue(ingest.SPOTIFY_ID.fullmatch(songs[0]["song_id"]))
        stmts = ingest.upsert_statements(songs[0], 10, "f", 1, None)  # the pipeline's own writer accepts it
        self.assertEqual(len(stmts), 3)

    def test_album_pagination(self):
        albums = [album(i) for i in range(25)]
        tracks = {a["id"]: [track(i)] for i, a in enumerate(albums)}
        fake = FakeSpotify(albums, tracks)
        cat, songs = run(fake)
        self.assertEqual(len(songs), 25)
        self.assertEqual(cat.calls, 3 + 25)  # 10+10+5 albums, one tracks call each
        offsets = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["offset"][0] for u in fake.urls[:3]]
        self.assertEqual(offsets, ["0", "10", "20"])

    def test_track_pagination_within_an_album(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(i) for i in range(120)]})
        cat, songs = run(fake)
        self.assertEqual(len(songs), 120)
        self.assertEqual(cat.calls, 1 + 3)

    def test_empty_artist_and_empty_album(self):
        cat, songs = run(FakeSpotify([], {}))
        self.assertEqual((songs, cat.calls), ([], 1))
        cat, songs = run(FakeSpotify([album(1)], {}))
        self.assertEqual((songs, cat.calls), ([], 2))

    def test_empty_page_with_no_next_ends(self):
        fake = FakeSpotify([], {}, script={1: (200, {}, {"items": [], "next": None})})
        cat, songs = run(fake)
        self.assertEqual((songs, cat.calls), ([], 1))

    def test_repeated_album_and_track_are_collapsed(self):
        a = album(1)
        fake = FakeSpotify([a, dict(a)], {a["id"]: [track(1), track(1)]})
        _, songs = run(fake)
        self.assertEqual(len(songs), 1)
        self.assertEqual(len(fake.urls), 2)

    def test_same_track_on_two_releases_is_one_record(self):
        fake = FakeSpotify([album(1), album(2)], {sid("L", 1): [track(1)], sid("L", 2): [track(1)]})
        self.assertEqual(len(run(fake)[1]), 1)

    def test_progress_and_call_count(self):
        lines = []
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1)]})
        cat = make(fake)
        at.artist_tracks(cat, ART, out=lines.append)
        self.assertTrue(any("1 releases listed" in ln for ln in lines))
        self.assertTrue(any("2 API calls" in ln for ln in lines))


class AlbumArtistFilter(unittest.TestCase):
    def setUp(self):
        self.own, self.va, self.feat = album(1), album(2, artists=((OTHER, "Various Artists"),), album_type="compilation"), \
            album(3, artists=((OTHER, "Someone"),), album_group="appears_on")
        self.tracks = {self.own["id"]: [track(1)],
                       self.va["id"]: [track(2, artists=((OTHER, "X"),)), track(3, artists=((ART, "Art"), (OTHER, "X")))],
                       self.feat["id"]: [track(4), track(5, artists=((OTHER, "Someone"),))]}

    def test_default_excludes_foreign_albums_and_does_not_fetch_them(self):
        fake = FakeSpotify([self.own, self.va, self.feat], self.tracks)
        cat, songs = run(fake)
        self.assertEqual([s["song_id"] for s in songs], [sid("T", 1)])
        self.assertEqual(cat.calls, 2)  # albums page + the one own album

    def test_opt_in_keeps_only_the_artists_own_tracks_of_foreign_albums(self):
        fake = FakeSpotify([self.own, self.va, self.feat], self.tracks)
        _, songs = run(fake, include_other_album_artists=True)
        self.assertEqual([s["song_id"] for s in songs], [sid("T", 1), sid("T", 3), sid("T", 4)])
        self.assertEqual(songs[1]["album_artist"], "Various Artists")

    def test_album_with_co_artist_counts_as_own(self):
        co = album(4, artists=((OTHER, "Other"), (ART, "Art")))
        fake = FakeSpotify([co], {co["id"]: [track(9)]})
        self.assertEqual(len(run(fake)[1]), 1)


class Malformed(unittest.TestCase):
    def test_items_not_a_list(self):
        for body in [{}, {"items": None}, {"items": "x"}, {"items": {}}]:
            with self.assertRaises(at.ArtistTracksError):
                run(FakeSpotify([], {}, script={1: (200, {}, body)}))

    def test_bad_items_are_skipped_not_fatal(self):
        a = album(1)
        junk = [None, 5, "x", {}, {"id": 7}, {"id": "short"}, {"id": "../etc/passwd" + "x" * 10}]
        fake = FakeSpotify([], {}, script={1: (200, {}, {"items": junk + [a], "next": None}),
                                           2: (200, {}, {"items": [
                                               None, {"id": "bad"}, track(1, duration_ms=0), track(2, duration_ms="9"),
                                               track(3, duration_ms=True), track(4, name=""), track(5, name=None),
                                               track(6, is_local=True), track(7, id=None), track(8)], "next": None})})
        _, songs = run(fake)
        self.assertEqual([s["song_id"] for s in songs], [sid("T", 8)])

    def test_odd_optional_fields_become_none(self):
        a = dict(album(1, release_date="x", images=[], name=None), artists="no")
        t = dict(track(1, track_number="3", disc_number=None), artists="no")
        fake = FakeSpotify([a], {a["id"]: [t]})
        # artists unknown everywhere -> not provably the artist's, so nothing is kept even when opted in
        _, songs = run(fake, include_other_album_artists=True)
        self.assertEqual(songs, [])
        a2 = album(2, release_date="x", images=[], name=None)
        fake = FakeSpotify([a2], {a2["id"]: [track(1, artists=((ART, "Art"),), track_number="3", disc_number=None)]})
        _, songs = run(fake)
        self.assertEqual((songs[0]["year"], songs[0]["cover_url"], songs[0]["album_name"], songs[0]["track_number"],
                          songs[0]["disc_number"]), (None, None, None, None, None))

    def test_year_formats(self):
        self.assertEqual([at._year(x) for x in ["2019", "2019-05", "2019-05-03", "", None, 2019, "19-05"]],
                         [2019, 2019, 2019, None, None, None, None])

    def test_cover_must_be_https(self):
        self.assertIsNone(at._cover({"images": [{"url": "http://x/y"}]}))
        self.assertIsNone(at._cover({"images": [{"url": 5}]}))

    def test_next_to_another_host_or_path_is_refused(self):
        for nxt in ["https://evil.example/v1/artists/%s/albums?offset=10" % ART,
                    "http://api.spotify.com/v1/artists/%s/albums?offset=10" % ART,
                    "https://api.spotify.com/v1/me/shows?offset=10", 7, ""]:
            fake = FakeSpotify([], {}, script={1: (200, {}, {"items": [album(1)], "next": nxt})})
            with self.assertRaises(at.ArtistTracksError, msg=repr(nxt)):
                run(fake)
            self.assertEqual(len(fake.urls), 1)  # the foreign URL was never requested

    def test_next_loop_is_stopped(self):
        loop = f"https://api.spotify.com/v1/artists/{ART}/albums?include_groups=album%2Csingle&limit=10&offset=0&market=US"
        fake = FakeSpotify([], {}, script={1: (200, {}, {"items": [], "next": loop})})
        with self.assertRaises(at.ArtistTracksError):
            run(fake)

    def test_error_status_is_reported_without_body(self):
        fake = FakeSpotify([], {}, script={1: (403, {}, {"error": {"message": CANARY}})})
        with self.assertRaises(at.ArtistTracksError) as cm:
            run(fake)
        self.assertIn("403", str(cm.exception))
        self.assertNotIn(CANARY, str(cm.exception))


class Retries(unittest.TestCase):
    def test_429_obeys_retry_after_then_succeeds(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1)]}, script={1: (429, {"retry-after": "7"}, {})})
        cat, songs = run(fake)
        self.assertEqual(len(songs), 1)
        self.assertEqual(cat.sleeps, [7])
        self.assertEqual(cat.calls, 3)  # the 429 counts as a call

    def test_429_without_retry_after_backs_off(self):
        fake = FakeSpotify([], {}, script={1: (429, {}, {}), 2: (429, {}, {})})
        cat = make(fake)
        at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertEqual(cat.sleeps, [1.0, 2.0])

    def test_429_is_bounded(self):
        fake = FakeSpotify([], {}, script={i: (429, {"retry-after": "1"}, {}) for i in range(1, 50)})
        with self.assertRaises(at.ArtistTracksError):
            run(fake)
        self.assertEqual(len(fake.urls), at.MAX_RETRIES + 1)

    def test_huge_retry_after_stops_instead_of_sleeping(self):
        fake = FakeSpotify([], {}, script={1: (429, {"retry-after": "86400"}, {})})
        cat = make(fake)
        with self.assertRaises(at.ArtistTracksError):
            at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertEqual(cat.sleeps, [])
        self.assertEqual(len(fake.urls), 1)

    def test_garbage_retry_after_falls_back_to_backoff(self):
        for ra in ["-1", "abc", "1.5", "", "9" * 20]:
            fake = FakeSpotify([], {}, script={1: (429, {"retry-after": ra}, {})})
            cat = make(fake)
            at.artist_tracks(cat, ART, out=lambda s: None)
            self.assertEqual(cat.sleeps, [1.0], msg=ra)

    def test_5xx_retried_other_4xx_not(self):
        fake = FakeSpotify([], {}, script={1: (503, {}, {})})
        cat, _ = run(fake)
        self.assertEqual(cat.calls, 2)
        fake = FakeSpotify([], {}, script={1: (404, {}, {})})
        with self.assertRaises(at.ArtistTracksError):
            run(fake)
        self.assertEqual(len(fake.urls), 1)

    def test_network_error_is_retried_and_bounded(self):
        n = []

        def boom(url, headers, timeout):
            n.append(1)
            raise at.ArtistTracksError("network error talking to Spotify (TimeoutError)")
        with self.assertRaises(at.ArtistTracksError):
            at.artist_tracks(make(boom), ART, out=lambda s: None)
        self.assertEqual(len(n), at.MAX_RETRIES + 1)

    def test_401_refreshes_the_token_once(self):
        toks = iter(["t1", "t2", "t3"])
        fake = FakeSpotify([], {}, script={1: (401, {}, {})})
        cat = at.Catalog(lambda: next(toks), [], fetch=fake, sleep=lambda s: None, min_interval=0, out=lambda s: None)
        at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertEqual(fake.auth, ["Bearer t1", "Bearer t2"])
        fake = FakeSpotify([], {}, script={1: (401, {}, {}), 2: (401, {}, {})})
        cat = at.Catalog(lambda: "t", [], fetch=fake, sleep=lambda s: None, min_interval=0, out=lambda s: None)
        with self.assertRaises(at.ArtistTracksError):
            at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertEqual(len(fake.urls), 2)

    def test_throttle_sleeps_between_calls(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1)]})
        sleeps = []
        cat = at.Catalog(lambda: "t", [], fetch=fake, sleep=sleeps.append, min_interval=5.0, out=lambda s: None)
        at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertEqual(len(sleeps), 1)  # the second call came right after the first
        self.assertGreater(sleeps[0], 4.0)


class HugeArtists(unittest.TestCase):
    def test_call_budget_stops_the_run(self):
        albums = [album(i) for i in range(100)]
        fake = FakeSpotify(albums, {a["id"]: [track(i)] for i, a in enumerate(albums)})
        cat = make(fake, max_calls=20)
        with self.assertRaises(at.ArtistTracksError) as cm:
            at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertIn("limit", str(cm.exception))
        self.assertEqual(len(fake.urls), 20)

    def test_album_cap(self):
        albums = [album(i) for i in range(30)]
        fake = FakeSpotify(albums, {})
        with self.assertRaises(at.ArtistTracksError):
            at.artist_tracks(make(fake), ART, max_albums=25, out=lambda s: None)

    def test_many_tracks_scale(self):
        albums = [album(i) for i in range(200)]
        fake = FakeSpotify(albums, {a["id"]: [track(i * 100 + j) for j in range(60)] for i, a in enumerate(albums)})
        cat, songs = run(fake)
        self.assertEqual(len(songs), 200 * 60)
        self.assertEqual(cat.calls, 20 + 200 * 2)


class NoSecrets(unittest.TestCase):
    def test_token_and_secret_never_in_errors(self):
        def leak(url, headers, timeout):
            raise at.ArtistTracksError(f"boom {CANARY} {SECRET}")
        cat = make(leak)
        cat._token = None
        with self.assertRaises(at.ArtistTracksError) as cm:
            at.artist_tracks(cat, ART, out=lambda s: None)
        self.assertNotIn(CANARY, str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))

    def test_cli_prints_count_only(self):
        fake = FakeSpotify([album(1)], {sid("L", 1): [track(1), track(2)]})
        out, err = io.StringIO(), io.StringIO()
        env = {"SPOTIFY_CLIENT_ID": "cid-canary", "SPOTIFY_CLIENT_SECRET": SECRET}
        real = at.Catalog

        class Fast(real):
            def __init__(self, token, secrets):
                super().__init__(token, secrets, fetch=fake, sleep=lambda s: None, min_interval=0)
        with mock.patch.dict(os.environ, env), mock.patch.object(at.tg_check, "load_env"), \
                mock.patch.object(at, "client_credentials_token", return_value=CANARY), \
                mock.patch.object(at, "Catalog", Fast), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = at.main([ART])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "2 tracks, 2 API calls")
        for secret in (CANARY, SECRET, "cid-canary"):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_cli_errors(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(at.tg_check, "load_env"), \
                contextlib.redirect_stderr(err):
            self.assertEqual(at.main([ART]), 1)
        self.assertIn("SPOTIFY_CLIENT_ID", err.getvalue())
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "cidZ98765", "SPOTIFY_CLIENT_SECRET": "secZ98765"}), \
                mock.patch.object(at.tg_check, "load_env"), contextlib.redirect_stderr(err):
            self.assertEqual(at.main(["not-an-artist"]), 1)
        self.assertIn("not a Spotify artist", err.getvalue())


class NoRedirect(unittest.TestCase):
    """Two real loopback servers: A answers every request with a 302 to B. B must never see a request, so neither
    the Bearer token nor the Basic header (client secret) can leave the first host."""

    def setUp(self):
        self.hits_b = []
        hits = self.hits_b

        class B(BaseHTTPRequestHandler):
            def _h(self):
                hits.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"items": [], "next": null, "access_token": "leaked"}')
            do_GET = do_POST = _h

            def log_message(self, *a):
                pass
        self.b = HTTPServer(("127.0.0.1", 0), B)
        target = f"http://127.0.0.1:{self.b.server_port}/x"

        class A(BaseHTTPRequestHandler):
            def _h(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
            do_GET = do_POST = _h

            def log_message(self, *a):
                pass
        self.a = HTTPServer(("127.0.0.1", 0), A)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.a, self.b)]
        for t in self.threads:
            t.start()
        self.url = f"http://127.0.0.1:{self.a.server_port}/v1/x"

    def tearDown(self):
        for s in (self.a, self.b):
            s.shutdown()
            s.server_close()

    def test_data_request_does_not_follow_a_redirect(self):
        status, _, body = at.default_fetch(self.url, {"Authorization": f"Bearer {CANARY}"}, 5)
        self.assertEqual((status, body), (302, {}))
        self.assertEqual(self.hits_b, [])

    def test_token_request_does_not_follow_a_redirect(self):
        with self.assertRaises(at.ArtistTracksError) as cm:
            at.client_credentials_token("cid", SECRET, 5, url=self.url)
        self.assertIn("302", str(cm.exception))
        self.assertEqual(self.hits_b, [])

    def test_catalog_reports_the_redirect_as_a_failure(self):
        cat = at.Catalog(lambda: CANARY, [SECRET], sleep=lambda s: None, min_interval=0, out=lambda s: None)
        with self.assertRaises(at.ArtistTracksError):
            cat.get(self.url)
        self.assertEqual(self.hits_b, [])


if __name__ == "__main__":
    unittest.main()

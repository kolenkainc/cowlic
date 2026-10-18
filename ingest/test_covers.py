"""Album covers from the YouTube Music match (#248). Offline: every download and upload is a fake (NoNetwork base)."""
import email.message
import http.server
import io
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import ingest
import match
from test_ingest import Fakes, song
from test_lastfm_source import NoNetwork

HOST = "lh3.googleusercontent.com"
BASE = f"https://{HOST}/abcDEF_-123"
SMALL = BASE + "=w60-h60-l90-rj"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100


def result(album="Hot Fuss", artist="The Killers", title="Mr. Brightside", dur=222, thumb=SMALL, vid="v1"):
    return {"videoId": vid, "title": title, "duration_seconds": dur,
            "artists": [{"name": artist, "id": "UCx"}], "album": {"name": album, "id": "MPREb_1"},
            "thumbnails": [{"url": thumb, "width": 60, "height": 60}]}


def cand(**kw):
    d = dict(thumbnail_base=BASE, album="Hot Fuss", artist_name="The Killers", artists="The Killers")
    d.update(kw)
    return match.Candidate("v1", "t", d["artists"], 200, 1.0, album=d["album"], artist_id="UCx",
                           artist_name=d["artist_name"], thumbnail_base=d["thumbnail_base"])


class CandidateFieldsTest(NoNetwork):
    class YTM:
        def __init__(self, r):
            self.r = r

        def search(self, q, filter=None, limit=None):
            return [self.r]

    def test_duration_mode_carries_cover_fields(self):
        c = match.find_match(self.YTM(result()), "Mr. Brightside", "The Killers", "Hot Fuss", 222)
        self.assertEqual((c.album, c.artist_id, c.artist_name, c.thumbnail_base), ("Hot Fuss", "UCx", "The Killers", BASE))
        self.assertEqual(c.mode, match.MODE_DURATION)

    def test_title_only_mode_carries_cover_fields(self):
        c = match.find_match(self.YTM(result()), "Mr. Brightside", "The Killers", None, None)
        self.assertEqual((c.album, c.thumbnail_base), ("Hot Fuss", BASE))
        self.assertEqual(c.mode, match.MODE_TITLE_ONLY)

    def test_missing_or_odd_fields_are_none(self):
        for r in ({"thumbnails": []}, {"thumbnails": [{"url": "http://x/y=w1"}]}, {"thumbnails": [{"url": "https://x/noeq"}]},
                  {"thumbnails": "x", "album": "x", "artists": ["x"]}, {}):
            e = match._result_extras(r)
            self.assertIsNone(e["thumbnail_base"])
            self.assertIsNone(e["album"])

    def test_last_equals_sign_is_the_suffix(self):
        self.assertEqual(match._result_extras(result(thumb="https://h/p=q=w60"))["thumbnail_base"], "https://h/p=q")


class GuardTest(NoNetwork):
    def url(self, wanted, artist="The Killers", **kw):
        return match.cover_url_from_match(wanted, artist, cand(**kw))

    def test_suffix_constant(self):
        self.assertEqual(match.COVER_SIZE_SUFFIX, "=w1200-h1200-l90-rj")
        self.assertEqual(self.url("Hot Fuss"), (BASE + "=w1200-h1200-l90-rj", "ok"))

    def test_same_album_with_punctuation_and_case(self):
        self.assertEqual(self.url("hot fuss!")[1], "ok")

    def test_missing_album_gets_no_cover(self):
        self.assertEqual(self.url(None), (None, "no_album"))
        self.assertEqual(self.url("  "), (None, "no_album"))

    def test_other_album_gets_no_cover(self):
        self.assertEqual(self.url("Sam's Town"), (None, "album_mismatch"))

    def test_other_edition_or_volume_gets_no_cover(self):
        self.assertEqual(self.url("Hot Fuss", album="Hot Fuss (Deluxe Edition)")[0], None)
        self.assertEqual(self.url("Greatest Hits Vol 1", album="Greatest Hits Vol 2")[0], None)
        self.assertEqual(self.url("Rock Anthems II", album="Rock Anthems III")[0], None)
        self.assertEqual(self.url("A Very Long Album Name Here", album="A Very Long Album Name Here Live")[0], None)

    def test_match_without_album_or_thumbnail(self):
        self.assertEqual(self.url("Hot Fuss", album=None), (None, "no_match_album"))
        self.assertEqual(self.url("Hot Fuss", thumbnail_base=None), (None, "no_thumbnail"))
        self.assertEqual(self.url("Hot Fuss", thumbnail_base="http://x/y"), (None, "no_thumbnail"))

    def test_other_artist_gets_no_cover(self):
        self.assertEqual(self.url("Hot Fuss", artist_name="Cover Band", artists="Cover Band"), (None, "artist_mismatch"))

    def test_latin_vs_cyrillic_artist_is_accepted(self):
        url, why = match.cover_url_from_match("Album", "Zemfira", cand(album="Album", artist_name="Земфира", artists="Земфира"))
        self.assertEqual(why, "ok")

    def test_leading_article_does_not_decide_the_artist(self):
        self.assertEqual(self.url("Hot Fuss", artist="Killers")[1], "ok")


class ProcessTrackCoverTest(NoNetwork):
    def go(self, s, m, covers=None, fail_cover=False):
        uploads = []
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d))
            deps = f.deps()

            def up(song_, tdir):
                uploads.append(song_)
                if fail_cover:
                    raise ingest.IngestError("cover fetch failed")
                return "cov1"
            deps.upload_cover, deps.find_match = up, lambda _s: m
            covers = {} if covers is None else covers
            notes = {}
            status = ingest.process_track(s, deps, set(), covers, Path(d), False, notes)
        return status, uploads, f.batches, covers, notes

    def test_cover_taken_from_the_match(self):
        status, up, batches, covers, notes = self.go(song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"), cand())
        self.assertEqual(status, "new")
        self.assertEqual(up[0]["cover_url"], BASE + "=w1200-h1200-l90-rj")
        self.assertTrue(up[0][ingest.COVER_FROM_MATCH])
        self.assertEqual(batches[0][1][1][-1], "cov1")
        self.assertTrue(notes["cover_from_match"])

    def test_spotify_cover_is_never_overwritten(self):
        s = song(1, cover_url="https://i.scdn.co/image/x", album_name="Hot Fuss", album_artist="The Killers")
        _, up, _, _, notes = self.go(s, cand())
        self.assertEqual(up[0]["cover_url"], "https://i.scdn.co/image/x")
        self.assertNotIn(ingest.COVER_FROM_MATCH, up[0])
        self.assertEqual(notes, {})

    def test_existing_album_cover_is_reused_without_a_lookup(self):
        s = song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers")
        al = ingest.album_id("The Killers", "Hot Fuss")
        _, up, batches, _, _ = self.go(s, cand(), covers={al: "old"})
        self.assertEqual(up, [])
        self.assertEqual(batches[0][1][1][-1], "old")

    def _refused(self, s, m):
        status, up, batches, covers, notes = self.go(s, m)
        self.assertEqual(status, "new")  # the track itself is stored
        self.assertEqual(up, [])
        self.assertIsNone(batches[0][1][1][-1])
        self.assertEqual(covers, {})
        self.assertTrue(notes["cover_skipped"])

    def test_album_mismatch_gets_no_cover(self):
        self._refused(song(1, cover_url=None, album_name="Sams Town", album_artist="The Killers"), cand())

    def test_unknown_album_gets_no_cover(self):
        self._refused(song(1, cover_url=None, album_name=ingest.UNKNOWN_ALBUM, album_artist="The Killers"), cand(album=ingest.UNKNOWN_ALBUM))
        self._refused(song(1, cover_url=None, album_name=None, album_artist="The Killers"), cand())

    def test_duration_mode_audio_match_but_cover_act_gets_no_cover(self):
        # same title and duration, another artist: the audio match is accepted (artist only breaks ties), the cover is not
        c = match.find_match(CandidateFieldsTest.YTM(result(artist="Cover Band")), "Mr. Brightside", "The Killers", "Hot Fuss", 222)
        self.assertIsNotNone(c)
        self._refused(song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"), c)

    def test_cover_failure_never_fails_the_track(self):
        status, up, batches, covers, _ = self.go(song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"),
                                                 cand(), fail_cover=True)
        self.assertEqual(status, "new")
        self.assertEqual(len(up), 1)
        self.assertIsNone(batches[0][1][1][-1])
        self.assertEqual(covers, {})

    def test_match_without_cover_fields_is_silent(self):
        status, up, _, _, notes = self.go(song(1, cover_url=None), SimpleNamespace(video_id="v"))
        self.assertEqual((status, up, notes), ("new", [], {}))


class FakeResponse:
    def __init__(self, body=JPEG, ctype="image/jpeg", chunks=None, tick=None):
        self.headers = email.message.Message()
        if ctype is not None:
            self.headers["Content-Type"] = ctype
        self.chunks = list(chunks) if chunks is not None else [body]
        self.tick = tick

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self.tick:
            self.tick()
        return self.chunks.pop(0) if self.chunks else b""

    read1 = read


class FakeOpener:
    def __init__(self, resp):
        self.resp, self.opened = resp, []

    def open(self, url, timeout=None):
        self.opened.append((url, timeout))
        return self.resp


class FetchCoverTest(NoNetwork):
    URL = BASE + "=w1200-h1200-l90-rj"

    def fetch(self, resp=None, url=None, clock=None):
        op = FakeOpener(resp or FakeResponse())
        with tempfile.TemporaryDirectory() as d:
            kw = {"clock": clock} if clock else {}
            p = ingest.fetch_cover(url or self.URL, Path(d) / "c.jpg", ingest.YTM_COVER_HOSTS, op, **kw)
            return p.read_bytes(), op

    def rejected(self, **kw):
        with self.assertRaises(ingest.IngestError):
            self.fetch(**kw)

    def test_good_cover(self):
        data, op = self.fetch()
        self.assertEqual(data, JPEG)
        self.assertEqual(op.opened[0][1], 30)

    def test_hosts_are_exactly_the_observed_thumbnail_hosts(self):
        self.assertEqual(ingest.YTM_COVER_HOSTS, {"yt3.googleusercontent.com", "lh3.googleusercontent.com", "yt3.ggpht.com"})

    def test_artist_thumbnail_on_ggpht_uses_the_same_checked_fetch(self):
        url = "https://yt3.ggpht.com/FAKEARTIST=w800-h800-p-l90-rj"
        data, op = self.fetch(url=url)
        self.assertEqual(data, JPEG)
        self.assertEqual(op.opened, [(url, 30)])
        self.rejected(url=url.replace("https:", "http:"))
        self.rejected(url=url, resp=FakeResponse(ctype="text/html"))
        self.rejected(url=url, resp=FakeResponse(body=b"not a JPEG"))

    def test_http_rejected(self):
        self.rejected(url="http://lh3.googleusercontent.com/x=w1")

    def test_other_hosts_rejected(self):
        for u in ("https://evil.example/x=w1", "https://lh3.googleusercontent.com.evil.example/x",
                  "https://evil.example/@lh3.googleusercontent.com/x", "https://lh3.googleusercontent.com@evil.example/x",
                  "https://user:p@lh3.googleusercontent.com/x", "https://lh3.googleusercontent.com:8443/x",
                  "https://lh3.googleusercontent.com:bad/x", "https://i.scdn.co/image/x", "https://localhost/x",
                  "https://yt3.ggpht.com.evil.example/x", "https://evil.yt3.ggpht.com/x",
                  "https://yt3.ggpht.com@evil.example/x", "https://user:p@yt3.ggpht.com/x",
                  "https://yt3.ggpht.com:8443/x", "https://yt3.ggpht.com:bad/x"):
            with self.subTest(u=u):
                self.rejected(url=u)

    def test_wrong_content_type_rejected(self):
        for ct in ("text/html", "image/png", None, "image/jpegx"):
            with self.subTest(ct=ct):
                self.rejected(resp=FakeResponse(ctype=ct))

    def test_content_type_with_parameters_and_case_ok(self):
        self.assertEqual(self.fetch(FakeResponse(ctype="Image/JPEG; charset=binary"))[0], JPEG)

    def test_wrong_magic_rejected(self):
        for body in (b"\x89PNG" + b"x" * 50, b"<html>", b"", b"\xff"):
            with self.subTest(body=body[:4]):
                self.rejected(resp=FakeResponse(body=body))

    def test_oversize_rejected_even_when_streamed(self):
        big = JPEG + b"x" * ingest.COVER_MAX_BYTES
        self.rejected(resp=FakeResponse(body=big))
        self.rejected(resp=FakeResponse(chunks=[JPEG + b"x" * (ingest.COVER_MAX_BYTES // 2)] * 3))

    def test_exactly_the_cap_is_ok(self):
        body = JPEG + b"x" * (ingest.COVER_MAX_BYTES - len(JPEG))
        self.assertEqual(len(self.fetch(FakeResponse(body=body))[0]), ingest.COVER_MAX_BYTES)

    def test_slow_download_rejected(self):
        t = [0.0]

        def clock():
            return t[0]
        resp = FakeResponse(chunks=[JPEG] * 100, tick=lambda: t.__setitem__(0, t[0] + 5))
        self.rejected(resp=resp, clock=clock)

    def test_userinfo_forms_rejected(self):
        for u in ("https://:@lh3.googleusercontent.com/x", "https://@lh3.googleusercontent.com/x"):
            with self.subTest(u=u):
                self.rejected(url=u)

    def test_failed_download_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "c.jpg"
            with self.assertRaises(ingest.IngestError):
                ingest.fetch_cover(self.URL, dest, ingest.YTM_COVER_HOSTS, FakeOpener(FakeResponse(ctype="text/html")))
            self.assertFalse(dest.exists())


class RedirectTest(NoNetwork):
    """The real opener of the YouTube route against a fake https handler that answers 302."""

    class Fake302(urllib.request.HTTPSHandler):
        seen: list = []

        def https_open(self, req):
            self.seen.append(req.full_url)
            h = email.message.Message()
            if len(self.seen) == 1:
                h["Location"] = self.location
                r = urllib.response.addinfourl(io.BytesIO(b""), h, req.full_url, 302)
                r.msg = "Found"
                return r
            h["Content-Type"] = "image/jpeg"
            r = urllib.response.addinfourl(io.BytesIO(JPEG), h, req.full_url, 200)
            r.msg = "OK"
            return r

    def run_to(self, location):
        handler = self.Fake302()
        handler.seen, handler.location = [], location
        opener = ingest._cover_opener(handler)
        with tempfile.TemporaryDirectory() as d, self.assertRaises(OSError):  # HTTPError is an OSError
            ingest.fetch_cover(FetchCoverTest.URL, Path(d) / "c.jpg", ingest.YTM_COVER_HOSTS, opener)
        return handler.seen

    def test_redirect_to_another_host_is_not_followed(self):
        self.assertEqual(len(self.run_to("https://evil.example/x.jpg")), 1)

    def test_redirect_to_http_is_not_followed(self):
        self.assertEqual(len(self.run_to("http://lh3.googleusercontent.com/x.jpg")), 1)

    def test_redirect_to_an_allowlisted_host_is_not_followed_either(self):
        self.assertEqual(len(self.run_to("https://yt3.googleusercontent.com/x.jpg")), 1)

    def test_default_opener_carries_the_no_redirect_handler(self):
        self.assertTrue(any(isinstance(h, ingest._NoRedirect) for h in ingest._cover_opener().handlers))


class SpotifyRouteUnchangedTest(NoNetwork):
    def test_spotify_url_is_not_restricted_to_the_youtube_hosts(self):
        calls = []

        class R(FakeResponse):
            def read(self, n=-1):
                calls.append(n)
                return b"anything"
        orig = ingest.urllib.request.urlopen
        ingest.urllib.request.urlopen = lambda url, timeout=None: R()
        try:
            with tempfile.TemporaryDirectory() as d:
                p = ingest.fetch_cover("https://i.scdn.co/image/x", Path(d) / "c.jpg")
                self.assertEqual(p.read_bytes(), b"anything")
        finally:
            ingest.urllib.request.urlopen = orig
        self.assertEqual(calls, [ingest.COVER_MAX_BYTES + 1])

    def test_spotify_url_must_still_be_https(self):
        with self.assertRaises(ingest.IngestError):
            ingest.fetch_cover("http://i.scdn.co/x", Path("/nonexistent/c.jpg"))


class RealDepsRoutingTest(NoNetwork):
    def route(self, s):
        seen = []

        def fake_fetch(url, dest, allowed_hosts=None, *a, **k):
            seen.append(allowed_hosts)
            dest.write_bytes(JPEG)
            return dest
        orig_fetch, orig_up = ingest.fetch_cover, ingest.tg_upload
        ingest.fetch_cover = fake_fetch
        ingest.tg_upload = lambda *a, **k: {"photo": [{"file_id": "big", "file_size": 9}]}
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(ingest.real_deps(None).upload_cover(s, Path(d)), "big")
        finally:
            ingest.fetch_cover, ingest.tg_upload = orig_fetch, orig_up
        return seen

    def test_match_cover_uses_the_allowlist(self):
        self.assertEqual(self.route({"cover_url": SMALL, ingest.COVER_FROM_MATCH: True}), [ingest.YTM_COVER_HOSTS])

    def test_spotify_cover_has_no_allowlist(self):
        self.assertEqual(self.route({"cover_url": "https://i.scdn.co/x"}), [None])


class SlowDripTest(unittest.TestCase):
    """A real loopback server (plain unittest.TestCase: NoNetwork would block the socket) that sends one byte per
    0.3 s. A blocking read(64 KiB) would never return; read1 hands each byte over so the deadline can fire."""

    def test_deadline_ends_a_slow_drip(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(5 * 1024 * 1024))
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.3)
                except OSError:
                    pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        out = {}

        def go():
            t0 = time.monotonic()
            try:
                with urllib.request.build_opener().open(f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=30) as r:
                    ingest._read_cover_body(r, time.monotonic, time.monotonic() + 1.5)
                out["r"] = "returned"
            except ingest.IngestError as e:
                out["r"] = str(e)
            out["t"] = time.monotonic() - t0
        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(10)
        self.assertEqual(out.get("r"), "cover download too slow", out or "still blocked after 10 s")
        self.assertLess(out["t"], 5)


class BoundaryTest(NoNetwork):
    """Similarity bars: pairs on both sides of each."""

    def album(self, a, b):
        return match.albums_match(a, b)

    def test_album_bar_is_0_9(self):
        self.assertEqual(round(match.SequenceMatcher(None, "abcdefghijklmnopqrst", "abcdefghijklmnopqrxx").ratio(), 2), 0.9)
        self.assertTrue(self.album("abcdefghijklmnopqrst", "abcdefghijklmnopqrxx"))  # exactly at the bar
        self.assertFalse(self.album("abcdefghijklmnopqrst", "abcdefghijklmnopqxxx"))  # 0.85
        self.assertFalse(self.album("night visions", "night visiting"))  # 0.82

    def test_artist_bar(self):
        self.assertEqual(round(match.artist_similarity("Eve", "Eva"), 2), 0.67)
        self.assertFalse(match.artists_match("Eve", ["Eva"]))
        self.assertTrue(match.artists_match("Eve", ["Eve"]))
        self.assertFalse(match.artists_match("abcdefghij", ["abcdefgxyz"]))  # 0.7, above the title-only bar, below ours
        self.assertTrue(match.artists_match("abcdefghij", ["abcdefghxy"]))  # 0.8, exactly at the bar

    def test_plural_only_differences_are_refused(self):
        self.assertFalse(self.album("Lover", "Lovers"))
        self.assertFalse(self.album("Lovers", "Lover"))
        self.assertFalse(match.artists_match("Slayer", ["Slayers"]))
        self.assertTrue(match.artists_match("Slayer", ["Slayer"]))

    def test_each_edition_word_alone(self):
        base = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
        for w in ("deluxe", "remastered", "live", "edition", "anniversary"):
            with self.subTest(w=w):
                self.assertGreaterEqual(match.SequenceMatcher(None, base, f"{base} {w}").ratio(), 0.9)  # ratio alone passes
                self.assertFalse(self.album(base, f"{base} {w}"))
        self.assertTrue(self.album(base, base))

    def test_joined_artists_field_is_used(self):
        m = cand(artist_name="Other Artist", artists="Other Artist, The Killers")
        self.assertEqual(match.cover_url_from_match("Hot Fuss", "The Killers", m)[1], "ok")
        m = cand(artist_name="Other Artist", artists="Other Artist, Third One")
        self.assertEqual(match.cover_url_from_match("Hot Fuss", "The Killers", m)[1], "artist_mismatch")


class CoverFailureKindsTest(ProcessTrackCoverTest):
    def go_raising(self, exc):
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d))
            deps = f.deps()

            def up(song_, tdir):
                raise exc
            deps.upload_cover, deps.find_match = up, lambda _s: cand()
            notes = {}
            s = song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers")
            status = ingest.process_track(s, deps, set(), {}, Path(d), False, notes)
            return status, f.batches, notes

    def test_oserror_and_httperror_never_fail_the_track(self):
        for exc in (OSError("net"), urllib.error.HTTPError("https://x", 302, "Found", {}, None), ValueError("v"),
                    KeyError("k"), ingest.IngestError("e")):
            with self.subTest(exc=type(exc).__name__):
                status, batches, notes = self.go_raising(exc)
                self.assertEqual(status, "new")
                self.assertIsNone(batches[0][1][1][-1])
                self.assertNotIn("cover_from_match", notes)  # counted only after a successful upload

    def test_counted_after_a_successful_upload(self):
        _, _, _, _, notes = self.go(song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"), cand())
        self.assertTrue(notes["cover_from_match"])

    def test_counts_and_summary_in_run(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d))
            deps = f.deps()
            deps.find_match = lambda s: cand()
            songs = [song(1, cover_url=None, album_name="Hot Fuss", album_artist="The Killers"),
                     song(2, cover_url=None, album_name="Other Album", album_artist="The Killers")]
            counts = ingest.run(songs, deps, set(), {}, Path(d), False)
        self.assertEqual((counts["cover_from_match"], counts["cover_skipped"]), (1, 1))
        self.assertIn("1 covers from the match, 1 without a cover", ingest.summary(counts))

import contextlib
import io
import json
import os
import stat
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ingest

GOOD_URL = "https://open.spotify.com/playlist/1HNODBooDQImveVtlDdJL4"
GOOD_URL2 = "https://open.spotify.com/playlist/" + "b" * 22
GOOD_URL3 = "https://open.spotify.com/album/" + "c" * 22
# valid prefix, bad tail: only an end-anchored full match rejects these
END_ANCHOR_BAD = (GOOD_URL + "/", GOOD_URL + "x", GOOD_URL + "?a b", GOOD_URL + "?a;b", GOOD_URL + "?")

SONG = {"name": "T", "artists": ["A"], "album_artist": "A", "album_name": "Al", "year": 2020,
        "song_id": "sp1", "track_number": 3, "disc_number": 1, "duration": 200}


class IngestTest(unittest.TestCase):
    def test_ids_stable_and_distinct(self):
        self.assertEqual(ingest.artist_id("Foo"), ingest.artist_id(" foo "))
        self.assertNotEqual(ingest.artist_id("Foo"), ingest.album_id("Foo", "Foo"))
        self.assertNotEqual(ingest.album_id("a", "bc"), ingest.album_id("ab", "c"))
        self.assertEqual(len(ingest.track_id("x")), 16)

    def test_size_filter(self):
        self.assertFalse(ingest.too_big(20 * 1024 * 1024))
        self.assertTrue(ingest.too_big(20 * 1024 * 1024 + 1))

    def test_sql_is_parameterized(self):
        stmts = ingest.upsert_statements(SONG, 1000, "fid", 7, "cov")
        self.assertEqual(len(stmts), 3)
        for sql, params in stmts:
            self.assertEqual(sql.count("?"), len(params))
            self.assertNotIn("Al", sql)  # values travel only as params
        # statement text is constant, independent of hostile input
        evil = dict(song(1), name="x'); DROP TABLE tracks;--", album_name="a'--", song_id="sp1")
        self.assertEqual([s for s, _ in ingest.upsert_statements(evil, 1, "f", 1, None)], [s for s, _ in stmts])
        self.assertIn("COALESCE", stmts[1][0])
        self.assertEqual(stmts[2][1][0], ingest.track_id("sp1"))

    def test_largest_photo(self):
        photos = [{"file_id": "s", "file_size": 10, "width": 1, "height": 1},
                  {"file_id": "b", "file_size": 99, "width": 2, "height": 2}]
        self.assertEqual(ingest.largest_photo_file_id(photos), "b")

    def test_missing_artist_and_album_tolerated(self):
        stmts = ingest.upsert_statements({"name": "T", "artists": [], "song_id": "sp1", "duration": 5}, 1, "f", 1, None)
        self.assertEqual(stmts[0][1][1], ingest.UNKNOWN_ARTIST)
        self.assertEqual(stmts[1][1][2], ingest.UNKNOWN_ALBUM)

    def test_dedupe_and_redact(self):
        self.assertEqual(len(ingest.dedupe([{"song_id": "a"}, {"song_id": "a"}, {"song_id": "b"}])), 2)
        self.assertEqual(ingest.redact("id=SEC1 x", ["SEC1", ""]), "id=*** x")


def sid(n):
    return f"{n:022d}"


class Fakes:
    def __init__(self, tmp, no_match=(), big=(), fail_upload=(), fail_d1=()):
        self.tmp, self.no_match, self.big, self.fail_upload, self.fail_d1 = tmp, set(no_match), set(big), \
            set(fail_upload), set(fail_d1)
        self.uploaded, self.deleted, self.batches = [], [], []

    def deps(self):
        def download(song, m, tdir):
            p = tdir / "a.mp3"
            p.write_bytes(b"x" * (ingest.MAX_BYTES + 1 if song["song_id"] in self.big else 10))
            return p

        def audio(song, path):
            if song["song_id"] in self.fail_upload:
                raise ingest.IngestError("Telegram API error 429: flood")
            self.uploaded.append(song["song_id"])
            return {"message_id": len(self.uploaded), "audio": {"file_id": "f"}}

        def d1(stmts):
            if stmts[2][1][1] in self.fail_d1:
                raise ingest.IngestError("D1 API error 500")
            self.batches.append(stmts)

        return ingest.Deps(lambda s: None if s["song_id"] in self.no_match else object(), download, audio,
                           lambda s, d: "cover", d1, self.deleted.append)


def song(n, **kw):
    return dict(SONG, song_id=sid(n), name=f"T{n}", url="u", **kw)


class RunTest(unittest.TestCase):
    def go(self, songs, existing=(), **kw):
        with tempfile.TemporaryDirectory() as d:
            f = Fakes(Path(d), **kw)
            existing = set(existing)
            counts = ingest.run(songs, f.deps(), existing, {}, Path(d), False)
            return f, counts, existing

    def test_skips_existing_and_duplicates(self):
        f, c, ex = self.go([song(1), song(2), song(2)], existing=[sid(1)])
        self.assertEqual((c["new"], c["exists"]), (1, 1))
        self.assertEqual(f.uploaded, [sid(2)])
        self.assertIn(sid(2), ex)

    def test_no_match_and_oversize_are_not_uploaded(self):
        f, c, _ = self.go([song(1), song(2)], no_match=[sid(1)], big=[sid(2)])
        self.assertEqual((c["no_match"], c["too_big"], c["failed"]), (1, 1, 0))
        self.assertEqual(f.uploaded, [])

    def test_failure_does_not_stop_next_track(self):
        f, c, _ = self.go([song(1), song(2)], fail_upload=[sid(1)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))
        self.assertEqual(f.uploaded, [sid(2)])

    def test_d1_failure_deletes_telegram_message_and_continues(self):
        f, c, ex = self.go([song(1), song(2)], fail_d1=[sid(1)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))
        self.assertEqual(f.deleted, [1])
        self.assertNotIn(sid(1), ex)

    def test_bad_song_id_fails_that_track_only(self):
        _, c, _ = self.go([dict(SONG, song_id="../x"), song(2)])
        self.assertEqual((c["failed"], c["new"]), (1, 1))

    def test_missing_fields_fail_only_that_track(self):
        _, c, _ = self.go([{"song_id": sid(1)}, song(2)])
        self.assertEqual(c["new"], 1)


# ---- fake spotdl: exercises spotdl_run / download / the Summary step without touching YouTube ----

CID, SECRET = "cid0123456789abcdefCID", "Sec0123456789abcdefSECRET0123"

# argv: <subcommand> ... --client-id ID --client-secret SECRET. Behaviour comes from FAKE_MODE; it writes no mp3.
FAKE_SPOTDL = r"""
import os, sys
a = sys.argv
cid, secret = a[a.index("--client-id") + 1], a[a.index("--client-secret") + 1]
mode = os.environ["FAKE_MODE"]
if mode == "block":
    print("yt_dlp.utils.DownloadError: ERROR: [youtube] x: Sign in to confirm you're not a bot", file=sys.stderr)
elif mode == "block_nonzero":
    print("yt_dlp.utils.DownloadError: ERROR: [youtube] x: Sign in to confirm you're not a bot", file=sys.stderr)
    sys.exit(1)
elif mode == "noisy":
    for i in range(100):
        print(f"line {i}")
elif mode == "upper":
    print(f"client {cid.upper()} secret {secret.upper()}")
elif mode == "wrapped":
    print(f"client {cid[:7]}\n{cid[7:]} secret {secret[:9]}\n  {secret[9:]}")
elif mode == "ansi":
    print(f"client {cid[:6]}\x1b[31m{cid[6:]}\x1b[0m secret \x1b[1m{secret[:4]}\x1b[0m{secret[4:]}")
elif mode == "argv":
    print(f"loglevel={a[a.index('--log-level') + 1]} columns={os.environ.get('COLUMNS')}")
elif mode == "debug_cause":
    # shape of spotdl's output for a failed download at --log-level DEBUG (padded to the console width)
    pad = " " * 300
    print("Processing query: https://music.youtube.com/watch?v=x|https://open.spotify.com/track/y")
    print(f"[13:10:00] DEBUG    MainThread - Downloader settings: {{'client_secret': '{secret}'}}{pad} downloader.py:129")
    print(f"[13:10:01] WARNING  MainThread - Some YouTube downloads require Deno. Run spotdl --download-deno{pad} deno.py:143")
    print(f"[13:10:01] DEBUG    MainThread - ERROR: [youtube] x: Video unavailable. This video is private{pad} base.py:408")
    print("AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=x")
elif mode.startswith("tail:"):
    # the yt-dlp exception is among the LAST lines, so it lands in the tail as well
    print(f"[13:10:01] DEBUG    MainThread - {mode.split(':', 1)[1]} base.py:408")
    print("AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=x")
elif mode.startswith("buried:"):
    # the yt-dlp exception is logged early; a rich traceback box (with a chained cause) follows it
    pad = " " * 300
    msg = mode.split(":", 1)[1]
    print(f"[13:10:01] DEBUG    MainThread - {msg}{pad} base.py:408")
    box = ["╭─ Traceback (most recent call last) ─╮", "│ 721 │ │ │ │ ), │", "│ ❱ 409 │ raise AudioProviderError(x) │",
           "╰─────────────────────────────────────╯"]
    for _ in range(40):
        for ln in box:
            print(ln + pad)
    print(f"AudioProviderError: {msg}")
    print("The above exception was the direct cause of the following exception:")
    for _ in range(10):
        for ln in box:
            print(ln + pad)
    print("AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=x")
    print("[13:10:02] DEBUG    MainThread - Took 8 seconds" + pad + " entry_point.py:182")
elif mode == "command":
    print("harmless\n::add-mask::topsecret\n  ::warning::x\n##[add-mask]y")
sys.exit(0)
"""


def download_log(work: Path, mode: str) -> str:
    """Runs ingest.run() with the real download() step against the fake spotdl; returns what it printed."""
    os.environ["FAKE_MODE"] = mode
    real = ingest.real_deps(None)
    deps = ingest.Deps(lambda s: SimpleNamespace(video_id="v"), real.download, None, None, None, None)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        counts = ingest.run([song(1)], deps, set(), {}, work, False)
    assert counts["failed"] == 1, counts
    return out.getvalue()


class FakeSpotdlCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        fake = self.work / "fake_spotdl.py"
        fake.write_text(FAKE_SPOTDL)
        for p in (mock.patch.object(ingest, "SPOTDL_CMD", [sys.executable, str(fake)]),
                  mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": CID, "SPOTIFY_CLIENT_SECRET": SECRET,
                                               "FAKE_MODE": ""})):
            p.start()
            self.addCleanup(p.stop)


class SpotdlOutputTest(FakeSpotdlCase):
    def test_exit_zero_without_file_surfaces_block_message(self):
        out = download_log(self.work, "tail:ERROR: [youtube] x: HTTP Error 403: Forbidden")
        self.assertIn("expected exactly one mp3 after download", out)
        self.assertIn("HTTP Error 403: Forbidden", out)

    def test_surfaced_tail_is_limited(self):
        out = download_log(self.work, "noisy")
        self.assertIn("line 99", out)
        self.assertIn("line 85", out)  # exactly TAIL_LINES (15) lines: 85..99
        self.assertNotIn("line 84", out)

    def test_redaction_forms(self):
        for mode in ("upper", "wrapped", "ansi"):
            with self.subTest(mode):
                out = download_log(self.work, mode)
                self.assertIn("***", out)
                flat = re.sub(r"\s+", "", out).lower()
                for secret in (CID, SECRET):
                    self.assertNotIn(secret.lower(), flat)
                self.assertNotIn("\x1b", out)

    def test_download_runs_at_debug_level_with_a_wide_console(self):
        out = download_log(self.work, "argv")
        self.assertIn("loglevel=DEBUG columns=2000", out)

    def test_real_yt_dlp_error_is_surfaced_without_noise(self):
        out = download_log(self.work, "debug_cause")
        self.assertIn("ERROR: [youtube] x: Video unavailable. This video is private base.py:408", out)
        self.assertIn("AudioProviderError: YT-DLP download error", out)
        self.assertNotIn("Downloader settings", out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("   ", out)  # padding collapsed

    def test_exception_line_far_from_the_end_is_surfaced_on_the_error_line(self):
        msg = "ERROR: [youtube] x: HTTP Error 403: Forbidden"
        out = download_log(self.work, "buried:" + msg)
        first = next(ln for ln in out.splitlines() if ln.startswith("ERROR "))
        self.assertIn(f"yt-dlp error: [13:10:01] DEBUG MainThread - {msg} base.py:408", first)
        self.assertIn(f"AudioProviderError: {msg}", first)
        self.assertNotIn("│", out)  # traceback box lines are dropped from the tail
        self.assertNotIn("╭", out)
        self.assertEqual(out.count("YT-DLP download error"), 1)  # only in the tail, not as a "cause"

    def test_exception_line_is_redacted(self):
        out = download_log(self.work, f"buried:ERROR: [youtube] x: bad {SECRET[:9]} {SECRET[9:]}")
        self.assertIn("***", out)
        self.assertNotIn(SECRET[:9], out)

    def test_cause_lines_ignore_unrelated_debug_records(self):
        lines = ["[13:10:00] DEBUG MainThread - Searching for x base.py:178", "plain text"]
        self.assertEqual(ingest.cause_lines(lines), [])

    SIGNED = (
        "ERROR: unable to download video data: HTTPSConnectionPool(host='rr1---sn-xx.googlevideo.com', port=443): "
        "Max retries exceeded with url: /videoplayback?expire=1760000000&ip=203.0.113.7&sig=AOq0QJ8wRAIgabc"
        "&pot=MnQ1abcdefghijklmnopqrstuvwxyz0123"
    )
    LEAKS = ("203.0.113.7", "expire=", "sig=", "pot=", "AOq0QJ8wRAIgabc", "MnQ1abcdefghijklmnopqrstuvwxyz0123")

    def assert_clean(self, out):
        for gone in self.LEAKS:
            self.assertNotIn(gone, out)

    def test_cause_lines_strip_signed_url_query_and_tokens(self):
        line = f"[13:10:00] DEBUG MainThread - {self.SIGNED} (Caused by NewConnectionError) base.py:408"
        out = " ".join(ingest.cause_lines([line]))
        self.assert_clean(out)
        self.assertIn("unable to download video data", out)
        self.assertIn("/videoplayback?<stripped>", out)
        self.assertIn("Max retries exceeded", out)

    def test_final_error_text_from_download_has_no_signed_url(self):
        # the tail path: the record is among the last lines, so it reaches the IngestError via the tail too
        out = download_log(self.work, "tail:" + self.SIGNED)
        self.assert_clean(out)
        self.assertIn("Max retries exceeded", out)
        self.assertIn("/videoplayback?<stripped>", out)

    def test_final_error_text_for_buried_record_has_no_signed_url(self):
        self.assert_clean(download_log(self.work, "buried:" + self.SIGNED))

    def test_tail_of_scrubs_every_line(self):
        out = ingest.tail_of(f"[13:10:00] DEBUG MainThread - {self.SIGNED} base.py:408\nplain", [])
        self.assert_clean(out)

    def test_watch_url_keeps_video_id_but_drops_other_params(self):
        f = ingest.scrub_cause
        self.assertEqual(f("see https://music.youtube.com/watch?v=abcDEF_-123"), "see https://music.youtube.com/watch?v=abcDEF_-123")
        self.assertEqual(f("https://x/watch?v=abcDEF_-123&list=PLsecretlistxx&si=zz"), "https://x/watch?v=abcDEF_-123&<stripped>")
        self.assertEqual(f("https://x/watch?list=PL1&v=abc"), "https://x/watch?v=abc&<stripped>")
        self.assertEqual(f("https://x/p?id=1#frag"), "https://x/p?<stripped>")

    def test_percent_encoded_query_is_stripped(self):
        out = ingest.scrub_cause("url: /videoplayback%3Fexpire%3D1760000000%26ip%3D203.0.113.7%26sig%3Dabc rest")
        self.assertNotIn("expire", out)
        self.assertNotIn("203.0.113.7", out)
        self.assertIn("/videoplayback?<stripped>", out)

    def test_path_style_and_param_ips_are_masked(self):
        f = ingest.scrub_cause
        self.assertNotIn("203.0.113.7", f("GET /videoplayback/ip/203.0.113.7/expire/1760000000/x failed"))
        self.assertNotIn("203.0.113.7", f("params;ip=203.0.113.7;other"))
        self.assertIn("ip=<ip>", f("params;ip=203.0.113.7;other"))
        self.assertNotIn("2001:db8", f("/ip/2001:db8:85a3::8a2e:370:7334/x"))

    def test_bare_ip_addresses_are_masked(self):
        f = ingest.scrub_cause
        self.assertEqual(f("connect to 203.0.113.7 failed"), "connect to <ip> failed")
        self.assertEqual(f("connect to [2001:db8::1] failed"), "connect to [<ip>] failed")
        self.assertEqual(f("addr 2001:0db8:85a3:0000:0000:8a2e:0370:7334 x"), "addr <ip> x")
        self.assertEqual(f("addr ::1 x"), "addr <ip> x")

    def test_timestamps_versions_and_hostnames_are_left_alone(self):
        for text in ("[13:10:00] DEBUG MainThread - hello base.py:408", "yt-dlp 2025.10.22 is old", "ffmpeg 7.1.1 ok",
                     "host rr1---sn-xx.googlevideo.com port 443", "see music.youtube.com/watch", "std::vector<int> fails"):
            self.assertEqual(ingest.scrub_cause(text), text)

    def test_long_opaque_tokens_are_masked_also_when_joined(self):
        f = ingest.scrub_cause
        self.assertEqual(f("bad QUJDREVGR0hJSktMTU5PUFFSU1RVVldY end"), "bad <token> end")
        jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4"
        self.assertEqual(f(f"auth {jwt} end"), "auth <token> end")
        self.assertEqual(f("path /a1b2c3d4e5/f6g7h8i9j0/k1l2m3n4o5 end"), "path /<token> end")

    def test_wrapped_url_continuation_is_masked(self):
        out = ingest.scrub_cause("&ip=203.0.113.7&sig=abc123&lsig=def&pot=xyz&Key=k1 tail")
        for gone in ("203.0.113.7", "abc123", "def", "xyz", "k1"):
            self.assertNotIn(gone, out)
        self.assertIn("tail", out)

    def test_html_escaped_separators_are_masked(self):
        out = ingest.scrub_cause("h/videoplayback&amp;ip=203.0.113.7&amp;sig=abc&amp;expire=1760000000 end")
        for gone in ("203.0.113.7", "sig=abc"):
            self.assertNotIn(gone, out)
        self.assertIn("end", out)

    def test_scrub_is_linear_on_long_input(self):
        import time
        t = time.monotonic()
        ingest.scrub_cause("a" * 100_000)
        ingest.scrub_cause("a." * 50_000)
        for _ in range(1000):
            ingest.scrub_cause("ab12" * 400)
        self.assertLess(time.monotonic() - t, 1.0)

    def test_scrub_keeps_ordinary_text(self):
        for text in ("is that ok?", "issue #12 failed", "C# is fine", "Sign in to confirm you're not a bot"):
            self.assertEqual(ingest.scrub_cause(text), text)

    def test_long_lines_are_capped(self):
        self.assertEqual(len(ingest.tail_of("ab " * 5000, [])), ingest.LINE_MAX)

    def test_redact_plain(self):
        self.assertEqual(ingest.redact(f"a {SECRET} b", [SECRET, "", CID]), "a *** b")

    def test_spotdl_output_cannot_issue_workflow_commands(self):
        out = download_log(self.work, "command")
        for line in out.splitlines():
            self.assertFalse(line.lstrip().startswith(("::", "##[")), line)
        self.assertIn("add-mask", out)  # still visible, just not at a line start

    def test_hostile_track_title_is_neutralised(self):
        out = io.StringIO()
        evil = dict(song(1), name="x\n::add-mask::hello\r::warning::y")
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            ingest.run([evil], Fakes(Path(d), fail_upload=[sid(1)]).deps(), set(), {}, Path(d), False)
        self.assertIn("add-mask", out.getvalue())
        for line in out.getvalue().splitlines():
            self.assertFalse(line.startswith("::"), line)


def fake_deno(directory: Path, output: str) -> None:
    exe = directory / "deno"
    exe.write_text(f"#!/bin/sh\necho '{output}'\n")
    exe.chmod(0o755)


class JsRuntimeTest(unittest.TestCase):
    def problem(self, output: str | None) -> str | None:
        with tempfile.TemporaryDirectory() as d:
            if output is not None:
                fake_deno(Path(d), output)
            with mock.patch.dict(os.environ, {"PATH": d}):
                return ingest.js_runtime_problem()

    def test_missing(self):
        self.assertIn("not found", self.problem(None))

    def test_too_old(self):
        self.assertIn("older than 2.3.0", self.problem("deno 2.2.9 (stable, release, x86_64-unknown-linux-gnu)"))

    def test_unreadable_version(self):
        self.assertIn("cannot read", self.problem("garbage"))

    def test_supported(self):
        for v in ("2.3.0", "2.9.5", "10.0.1"):
            self.assertIsNone(self.problem(f"deno {v} (stable, release, x86_64-unknown-linux-gnu)"), v)


class CloudflareTokenTest(unittest.TestCase):
    ENV = {"CLOUDFLARE_ACCOUNT_ID": "acct", "D1_DATABASE_ID": "db"}

    def test_d1_uses_only_the_narrow_token(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return io.BytesIO(b'{"success": true, "result": []}')

        env = {**self.ENV, "INGEST_CLOUDFLARE_API_TOKEN": "narrow-fake", "CLOUDFLARE_API_TOKEN": "broad-fake"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", fake_urlopen):
            ingest.d1_batch([("SELECT 1", [])])
        self.assertEqual(seen["auth"], "Bearer narrow-fake")

    def test_no_silent_fallback_to_the_broad_token(self):
        env = {**self.ENV, "CLOUDFLARE_API_TOKEN": "broad-fake"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen") as opened:
            os.environ.pop("INGEST_CLOUDFLARE_API_TOKEN", None)
            with self.assertRaises(KeyError):
                ingest.d1_batch([("SELECT 1", [])])
        opened.assert_not_called()

    def test_required_env_names_the_narrow_token_not_the_broad_one(self):
        self.assertIn("INGEST_CLOUDFLARE_API_TOKEN", ingest.REQUIRED_ENV)
        self.assertNotIn("CLOUDFLARE_API_TOKEN", ingest.REQUIRED_ENV)

    def test_missing_narrow_token_is_reported_by_name_even_with_the_broad_one(self):
        env = {k: "x" for k in ingest.REQUIRED_ENV if k != "INGEST_CLOUDFLARE_API_TOKEN"}
        env["CLOUDFLARE_API_TOKEN"] = "broad-fake"
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", ["ingest.py", GOOD_URL]), \
                contextlib.redirect_stdout(out):
            os.environ.pop("INGEST_CLOUDFLARE_API_TOKEN", None)
            self.assertEqual(ingest.main(), 1)
        self.assertIn("INGEST_CLOUDFLARE_API_TOKEN", out.getvalue())
        self.assertNotIn("broad-fake", out.getvalue())

    def test_spotdl_never_inherits_either_cloudflare_token(self):
        self.assertLessEqual({"INGEST_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN"}, ingest.SPOTDL_HIDDEN_ENV)


class SourcesTest(unittest.TestCase):
    def test_parses_list_with_comments_and_blank_lines(self):
        text = f"# c\n\n- {GOOD_URL}\n  - {GOOD_URL2}?si=abc123\n"
        self.assertEqual(ingest.parse_sources(text), [GOOD_URL, GOOD_URL2 + "?si=abc123"])

    def test_duplicates_collapse_keeping_order(self):
        self.assertEqual(ingest.parse_sources(f"- {GOOD_URL}\n- {GOOD_URL2}\n- {GOOD_URL}\n"), [GOOD_URL, GOOD_URL2])

    def test_invalid_entries_fail_with_line_number(self):
        for bad in ("- http://open.spotify.com/playlist/" + "a" * 22,       # not https
                    "- https://open.spotify.com.evil.test/playlist/" + "a" * 22,
                    "- https://open.spotify.com/playlist/short",
                    "- https://open.spotify.com/track/" + "a" * 22,
                    f"- {GOOD_URL} # trailing comment",
                    f"- {GOOD_URL} {GOOD_URL2}",
                    f"{GOOD_URL}", "key: value", f"-{GOOD_URL}",
                    *(f"- {u}" for u in END_ANCHOR_BAD)):
            with self.subTest(bad=bad), self.assertRaisesRegex(ingest.IngestError, "sources line 2"):
                ingest.parse_sources(f"- {GOOD_URL}\n{bad}\n")

    def test_empty_file_is_an_error(self):
        for text in ("", "# only comments\n\n"):
            with self.assertRaises(ingest.IngestError):
                ingest.parse_sources(text)

    def test_the_repo_sources_file_is_valid_and_starts_with_the_test_playlist(self):
        urls = ingest.parse_sources((Path(__file__).resolve().parent.parent / "sources.yml").read_text("utf-8"))
        self.assertEqual(urls[0], GOOD_URL)

    def test_cli_url_uses_the_same_validation(self):
        self.assertEqual(ingest.validate_playlist_url(GOOD_URL), GOOD_URL)
        for bad in ("https://example.invalid/playlist", *END_ANCHOR_BAD):
            with self.subTest(bad=bad), self.assertRaises(ingest.IngestError):
                ingest.validate_playlist_url(bad)


class RunPlaylistsTest(unittest.TestCase):
    def test_one_failing_playlist_does_not_hide_the_others(self):
        songs = {GOOD_URL: [song(1)], GOOD_URL2: [song(2)], GOOD_URL3: [song(3)]}

        def fetch(url):
            if url == GOOD_URL2:
                raise ingest.IngestError("spotdl save failed")
            return songs[url]

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d), fail_upload=[sid(3)])
            res = ingest.run_playlists([GOOD_URL, GOOD_URL2, GOOD_URL3], fetch, f.deps(), set(), {}, Path(d), False)
        self.assertEqual(f.uploaded, [sid(1)])
        self.assertEqual(res[GOOD_URL]["new"], 1)
        self.assertIsNone(res[GOOD_URL2])
        self.assertEqual(res[GOOD_URL3]["failed"], 1)
        lines = out.getvalue()
        self.assertEqual(sum(1 for ln in lines.splitlines() if ln.startswith("playlist ")), 3)
        self.assertIn(f"playlist {GOOD_URL2}: FAILED", lines)

    def run_main(self, argv, fetch_fails=False):
        env = {k: "x" for k in ingest.REQUIRED_ENV}
        out = io.StringIO()

        def fake_run_playlists(urls, *a):
            return {u: (None if fetch_fails else {"new": 0, "exists": 1, "no_match": 0, "too_big": 0, "failed": 0})
                    for u in urls}

        with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", ["ingest.py", *argv]), \
                mock.patch.object(ingest, "d1_query", return_value=[]), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "real_deps", return_value=None), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                mock.patch.object(ingest, "run_playlists", fake_run_playlists), \
                contextlib.redirect_stdout(out):
            return ingest.main(), out.getvalue()

    def test_main_sources_file_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sources.yml"
            p.write_text(f"- {GOOD_URL}\n- {GOOD_URL2}\n", encoding="utf-8")
            self.assertEqual(self.run_main(["--sources", str(p)])[0], 0)
            self.assertEqual(self.run_main(["--sources", str(p)], fetch_fails=True)[0], 1)
            p.write_bytes(b"\xef\xbb\xbf" + f"- {GOOD_URL}\n".encode())  # BOM must not break line 1
            self.assertEqual(self.run_main(["--sources", str(p)])[0], 0)
            p.write_text(f"- {GOOD_URL}\n- nonsense\n", encoding="utf-8")
            code, out = self.run_main(["--sources", str(p)])
            self.assertEqual(code, 1)
            self.assertIn("sources line 2", out)
            self.assertEqual(self.run_main(["--sources", str(Path(d) / "missing.yml")])[0], 1)

    def test_main_explicit_url_still_works_and_is_validated(self):
        self.assertEqual(self.run_main([GOOD_URL])[0], 0)
        code, out = self.run_main(["https://example.invalid/x"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)

    def test_main_requires_exactly_one_source(self):
        for argv in ([], [GOOD_URL, "--sources", "x.yml"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_main(argv)


class YouTubeSignInTest(FakeSpotdlCase):
    """A sign-in challenge ends the whole run: no retry, remaining tracks and playlists skipped, distinct exit."""

    def run_two_playlists(self, mode):
        os.environ["FAKE_MODE"] = mode
        real = ingest.real_deps(None)
        searched = []

        def find(s):
            searched.append(s["song_id"])
            return SimpleNamespace(video_id="v")

        deps = ingest.Deps(find, real.download, None, None, None, None)
        fetched = []

        def fetch(url):
            fetched.append(url)
            return [song(1), song(2)]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                ingest.run_playlists([GOOD_URL, GOOD_URL2], fetch, deps, set(), {}, self.work, False)
                raised = False
            except ingest.YouTubeSignInRequired:
                raised = True
        return raised, searched, fetched, out.getvalue()

    def test_challenge_skips_remaining_tracks_and_playlists(self):
        raised, searched, fetched, out = self.run_two_playlists("block")
        self.assertTrue(raised)
        self.assertEqual(searched, [sid(1)])  # one download attempt: no retry, track 2 never tried
        self.assertEqual(fetched, [GOOD_URL])  # second playlist never fetched

    def test_other_download_errors_do_not_end_the_run(self):
        raised, searched, fetched, out = self.run_two_playlists("debug_cause")
        self.assertFalse(raised)
        self.assertEqual(searched, [sid(1), sid(2), sid(1), sid(2)])
        self.assertEqual(fetched, [GOOD_URL, GOOD_URL2])

    def test_nonzero_spotdl_exit_with_the_challenge_is_recognised(self):
        os.environ["FAKE_MODE"] = "block_nonzero"
        with self.assertRaises(ingest.YouTubeSignInRequired):
            ingest.spotdl_run(["download", "x"], self.work)

    def test_counts_and_not_attempted_are_logged_and_carried(self):
        # playlist 1 (3 tracks) is fine; playlist 2 (4 tracks): track 1 new, track 2 challenged; playlist 3 untouched
        urls = [GOOD_URL, GOOD_URL2, GOOD_URL3]
        songs = {GOOD_URL: [song(1), song(2), song(3)], GOOD_URL2: [song(4), song(5), song(6), song(7)]}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d))
            deps = f.deps()
            inner = deps.download

            def download(s, m, tdir):
                if s["song_id"] == sid(5):
                    raise ingest.YouTubeSignInRequired("x")
                return inner(s, m, tdir)

            deps.download = download
            with self.assertRaises(ingest.YouTubeSignInRequired) as cm:
                ingest.run_playlists(urls, lambda u: songs[u], deps, set(), {}, Path(d), False)
        e = cm.exception
        self.assertEqual((e.counts["new"], e.counts["failed"]), (1, 0))
        self.assertEqual((e.tracks_not_attempted, e.playlists_not_attempted), (2, 1))
        text = out.getvalue()
        self.assertIn(f"playlist {GOOD_URL}: 3 new", text)
        self.assertIn(f"playlist {GOOD_URL2}: interrupted by youtube-sign-in: 1 new", text)
        self.assertIn("so far: 4 new, 0 already present, 0 no match, 0 too big, 0 failed; not attempted: 2 more "
                      "track(s) of the current playlist (plus the track that was challenged), 1 playlist(s) "
                      "not started", text)

    def test_detection_matches_real_captured_lines(self):
        for line in ("ERROR: [youtube] x: Sign in to confirm you're not a bot",
                     "[13:10:01] DEBUG MainThread - ERROR: [youtube] x: Sign in to confirm you’re not a bot. "
                     "Use --cookies-from-browser base.py:408",
                     "yt_dlp.utils.DownloadError: ERROR: [youtube] x: SIGN IN TO CONFIRM YOU'RE NOT A BOT"):
            with self.subTest(line=line):
                self.assertTrue(ingest.is_sign_in_challenge([line]))
        for line in ("ERROR: [youtube] x: Video unavailable. This video is private",
                     "ERROR: [youtube] x: Sign in to confirm your age"):
            with self.subTest(line=line):
                self.assertFalse(ingest.is_sign_in_challenge([line]))

    def run_main(self, exc):
        env = {k: "x" for k in ingest.REQUIRED_ENV}
        out = io.StringIO()

        def boom(*a):
            raise exc

        with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", ["ingest.py", GOOD_URL]), \
                mock.patch.object(ingest, "d1_query", return_value=[]), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "real_deps", return_value=None), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                mock.patch.object(ingest, "run_playlists", boom), \
                contextlib.redirect_stdout(out):
            return ingest.main(), out.getvalue()

    def test_main_exits_with_distinct_code_and_one_clear_line(self):
        code, out = self.run_main(ingest.YouTubeSignInRequired("x"))
        self.assertEqual(code, ingest.EXIT_YOUTUBE_SIGN_IN)
        self.assertNotIn(code, (0, 1, 2))
        lines = [ln for ln in out.splitlines() if "youtube-sign-in" in ln]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("ERROR youtube-sign-in:"))
        self.assertIn("wait and re-run later", lines[0])
        self.assertIn("do not add cookies", lines[0])
        self.assertEqual(ingest.FAILURE_YOUTUBE_SIGN_IN, "youtube-sign-in")
        self.assertNotIn("done:", out)


class ResultJsonTest(unittest.TestCase):
    """--result-json: counts and class names only, written atomically with mode 0600."""
    C1 = {"new": 2, "exists": 1, "no_match": 1, "too_big": 0, "failed": 0}
    C2 = {"new": 0, "exists": 3, "no_match": 0, "too_big": 1, "failed": 2}

    def test_success_result(self):
        r = ingest.build_result([GOOD_URL, GOOD_URL2], {GOOD_URL: self.C1, GOOD_URL2: self.C2}, None, 0)
        self.assertEqual((r["failure_class"], r["exit_code"]), ("none", 0))
        self.assertEqual([p["status"] for p in r["playlists"]], ["done", "done"])
        self.assertEqual(r["playlists"][0]["added"], 2)
        self.assertEqual(r["playlists"][0]["skipped"], 1)
        self.assertEqual((r["counts"]["added"], r["counts"]["skipped"], r["counts"]["failed"]), (2, 4, 2))

    def test_partial_and_unreadable_playlist(self):
        r = ingest.build_result([GOOD_URL, GOOD_URL2], {GOOD_URL: self.C1, GOOD_URL2: None}, None, 1)
        self.assertEqual(r["failure_class"], "partial")
        self.assertEqual(r["playlists"][1]["status"], "unreadable")
        self.assertEqual(r["counts"]["playlists_unreadable"], 1)

    def test_config_failure_before_any_playlist_ran(self):
        r = ingest.build_result([GOOD_URL], None, None, 1)
        self.assertEqual(r["failure_class"], "config")
        self.assertEqual(r["playlists"][0]["status"], "not_attempted")
        self.assertEqual(ingest.build_result([], None, None, 1)["playlists"], [])

    def test_youtube_sign_in_result_uses_the_exception_counts(self):
        e = ingest.YouTubeSignInRequired("x")
        e.counts, e.tracks_not_attempted, e.playlists_not_attempted = dict(self.C1), 2, 1
        e.results = {GOOD_URL: self.C2}
        r = ingest.build_result([GOOD_URL, GOOD_URL2, GOOD_URL3], None, e, ingest.EXIT_YOUTUBE_SIGN_IN)
        self.assertEqual((r["failure_class"], r["exit_code"]), ("youtube-sign-in", 3))
        self.assertEqual([p["status"] for p in r["playlists"]], ["done", "interrupted", "not_attempted"])
        self.assertEqual((r["playlists"][1]["added"], r["playlists"][1]["not_attempted"]), (2, 3))  # 2 + challenged
        self.assertEqual((r["counts"]["added"], r["counts"]["not_attempted"], r["counts"]["playlists_not_attempted"]),
                         (2, 3, 1))

    def test_sign_in_while_fetching_has_no_track_counts(self):
        e = ingest.YouTubeSignInRequired("x")  # counts None: raised by the playlist fetch
        r = ingest.build_result([GOOD_URL], None, e, 3)
        self.assertEqual((r["playlists"][0]["status"], r["counts"]["not_attempted"]), ("interrupted", 0))

    def test_playlist_url_query_is_dropped_and_nothing_else_leaks(self):
        url = GOOD_URL + "?si=abcdef0123456789"
        r = ingest.build_result([url], {url: self.C1}, None, 0)
        self.assertEqual(r["playlists"][0]["url"], GOOD_URL)
        self.assertNotIn("si=", json.dumps(r))

    def test_write_is_private_atomic_and_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            ingest.write_result(p, {"failure_class": "none"})
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(json.loads(p.read_text()), {"failure_class": "none"})
            self.assertEqual([x.name for x in Path(d).iterdir()], ["r.json"])  # no temp file left
            with mock.patch("os.replace", side_effect=OSError("boom")), contextlib.redirect_stdout(io.StringIO()) as out:
                ingest.write_result(p, {"failure_class": "partial"})
            self.assertEqual(json.loads(p.read_text()), {"failure_class": "none"})  # old content intact
            self.assertEqual([x.name for x in Path(d).iterdir()], ["r.json"])
            self.assertIn("WARN could not write the result file", out.getvalue())

    def run_main(self, argv, run_playlists):
        env = {k: "x" for k in ingest.REQUIRED_ENV}
        with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", ["ingest.py", *argv]), \
                mock.patch.object(ingest, "d1_query", return_value=[]), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "real_deps", return_value=None), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                mock.patch.object(ingest, "run_playlists", run_playlists), \
                contextlib.redirect_stdout(io.StringIO()):
            return ingest.main()

    def test_main_writes_the_file_in_every_outcome(self):
        ok = {"new": 1, "exists": 0, "no_match": 0, "too_big": 0, "failed": 0}

        def sign_in(urls, *a):
            e = ingest.YouTubeSignInRequired("x")
            e.counts, e.results = dict(ok), {}
            raise e

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            res = ["--result-json", str(p)]
            self.assertEqual(self.run_main([GOOD_URL, *res], lambda urls, *a: {u: ok for u in urls}), 0)
            self.assertEqual(json.loads(p.read_text())["failure_class"], "none")
            self.assertEqual(self.run_main([GOOD_URL, *res], lambda urls, *a: {u: None for u in urls}), 1)
            self.assertEqual(json.loads(p.read_text())["failure_class"], "partial")
            self.assertEqual(self.run_main([GOOD_URL, *res], sign_in), 3)
            self.assertEqual(json.loads(p.read_text())["failure_class"], "youtube-sign-in")
            self.assertEqual(self.run_main(["https://example.invalid/x", *res], None), 1)
            self.assertEqual(json.loads(p.read_text())["failure_class"], "config")
            p.unlink()
            self.assertEqual(self.run_main([GOOD_URL], lambda urls, *a: {u: ok for u in urls}), 0)  # option is optional
            self.assertFalse(p.exists())

    def test_a_crash_writes_no_file(self):
        def boom(*a):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            with self.assertRaises(RuntimeError):
                self.run_main([GOOD_URL, "--result-json", str(p)], boom)
            self.assertFalse(p.exists())


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class LimitsTest(unittest.TestCase):
    """--max-new / --max-minutes: a budget for one run; skipped tracks never use it."""

    def go(self, songs, existing=(), no_match=(), fail_upload=(), state=None, **limit_kw):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d), no_match=no_match, fail_upload=fail_upload)
            limits = ingest.RunLimits(**limit_kw)
            counts = ingest.run(songs, f.deps(), set(existing), {}, Path(d), False, limits, state)
        return f, counts, limits, out.getvalue()

    def test_without_limits_nothing_changes(self):
        f, c, limits, _ = self.go([song(n) for n in range(1, 8)])
        self.assertEqual(c["new"], 7)
        self.assertIsNone(limits.stop_reason)

    def test_cap_stops_the_run_and_records_the_rest(self):
        f, c, limits, _ = self.go([song(n) for n in range(1, 6)], max_new=2)
        self.assertEqual(f.uploaded, [sid(1), sid(2)])
        self.assertEqual((limits.stop_reason, limits.attempted, limits.not_attempted), ("cap", 2, 3))

    def test_already_present_tracks_do_not_use_the_cap(self):
        f, c, limits, _ = self.go([song(n) for n in range(1, 6)], existing=[sid(1), sid(2)], max_new=1)
        self.assertEqual(f.uploaded, [sid(3)])
        self.assertEqual((c["exists"], c["new"], limits.stop_reason), (2, 1, "cap"))

    def test_no_match_does_not_use_the_cap_but_failures_do(self):
        f, c, limits, _ = self.go([song(n) for n in range(1, 5)], no_match=[sid(1)], max_new=1)
        self.assertEqual((c["no_match"], f.uploaded, limits.stop_reason), (1, [sid(2)], "cap"))
        f, c, limits, _ = self.go([song(n) for n in range(1, 5)], fail_upload=[sid(1)], max_new=1)
        self.assertEqual((c["failed"], f.uploaded, limits.stop_reason), (1, [], "cap"))

    def test_a_cap_that_is_not_reached_is_done(self):
        _, c, limits, _ = self.go([song(1), song(2)], max_new=2)
        self.assertEqual((c["new"], limits.stop_reason), (2, None))  # exactly the cap, nothing left: done

    def test_time_limit_stops_before_the_next_track(self):
        clock = Clock()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d))
            deps = f.deps()
            inner = deps.download

            def slow(s, m, tdir):
                clock.t += 30  # each download takes 30 s
                return inner(s, m, tdir)

            deps.download = slow
            limits = ingest.RunLimits(max_minutes=1, clock=clock)
            ingest.run([song(n) for n in range(1, 6)], deps, set(), {}, Path(d), False, limits)
        self.assertEqual(f.uploaded, [sid(1), sid(2)])
        self.assertEqual((limits.stop_reason, limits.not_attempted), ("time", 3))

    def test_cap_is_shared_by_all_playlists_and_logged(self):
        songs = {GOOD_URL: [song(1), song(2)], GOOD_URL2: [song(3)]}
        fetched, out = [], io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d))
            limits = ingest.RunLimits(max_new=2)
            res = ingest.run_playlists([GOOD_URL, GOOD_URL2], lambda u: fetched.append(u) or songs[u], f.deps(),
                                       set(), {}, Path(d), False, limits)
        self.assertEqual((fetched, f.uploaded), ([GOOD_URL], [sid(1), sid(2)]))  # 2nd playlist not even fetched
        self.assertEqual(list(res), [GOOD_URL])
        self.assertIn("stopped: cap: --max-new 2 reached (2 attempted)", out.getvalue())
        self.assertIn("1 playlist(s) not started", out.getvalue())
        r = ingest.build_result([GOOD_URL, GOOD_URL2], res, None, 0, limits)
        self.assertEqual([p["status"] for p in r["playlists"]], ["done", "not_attempted"])
        self.assertEqual(r["stop_reason"], "cap")

    def test_stop_inside_a_playlist_is_reported_as_stopped(self):
        songs = {GOOD_URL: [song(n) for n in range(1, 5)], GOOD_URL2: [song(9)]}
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            f = Fakes(Path(d))
            limits = ingest.RunLimits(max_new=1)
            res = ingest.run_playlists([GOOD_URL, GOOD_URL2], lambda u: songs[u], f.deps(), set(), {}, Path(d),
                                       False, limits)
        r = ingest.build_result([GOOD_URL, GOOD_URL2], res, None, 0, limits)
        self.assertEqual([p["status"] for p in r["playlists"]], ["stopped", "not_attempted"])
        self.assertEqual((r["playlists"][0]["added"], r["playlists"][0]["not_attempted"]), (1, 3))
        self.assertEqual((r["stop_reason"], r["failure_class"], r["counts"]["not_attempted"]), ("cap", "none", 3))
        json.dumps(r)

    def test_limit_options_are_validated(self):
        for bad in ("0", "-1", "x", "1.5", "nan"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(sys, "argv", ["ingest.py", GOOD_URL, "--max-new", bad]):
                ingest.main()
        for bad in ("0", "-1", "inf", "nan"):
            with self.subTest(minutes=bad), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(sys, "argv", ["ingest.py", GOOD_URL, "--max-minutes", bad]):
                ingest.main()


class FixedToday:
    def __init__(self, day="2026-10-10"):
        self.day = ingest.datetime.date.fromisoformat(day)

    def __call__(self):
        return self.day


class StateTest(unittest.TestCase):
    """The local state file: cooldown for no_match/failed tracks, atomic, never fatal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "cowlic" / "ingest-state.json"
        self.today = FixedToday()

    def state(self, **days):
        return ingest.RunState.load(self.path, days or None, self.today)

    def go(self, songs, state, existing=(), dry=False, limits=None, **kw):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            f = Fakes(Path(d), **kw)
            counts = ingest.run(songs, f.deps(), set(existing), {}, Path(d), dry, limits, state)
        return f, counts, out.getvalue()

    def test_missing_file_starts_empty_without_a_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st = self.state()
        self.assertEqual((st.entries, out.getvalue()), ({}, ""))
        self.assertFalse(self.path.exists())  # nothing is written until something is learned

    def test_no_match_and_failed_are_remembered_with_the_default_periods(self):
        st = self.state()
        f, c, _ = self.go([song(1), song(2), song(3)], st, no_match=[sid(1)], fail_upload=[sid(2)])
        self.assertEqual((c["no_match"], c["failed"], c["new"]), (1, 1, 1))
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["tracks"], {sid(1): {"status": "no_match", "retry_after": "2026-11-09"},
                                          sid(2): {"status": "failed", "retry_after": "2026-10-13"}})
        self.assertEqual(ingest.DEFAULT_RETRY_NO_MATCH_DAYS, 30)
        self.assertEqual(ingest.DEFAULT_RETRY_FAILED_DAYS, 3)

    def test_periods_are_configurable_and_zero_means_always_retry(self):
        st = self.state(no_match=5, failed=0)
        self.go([song(1), song(2)], st, no_match=[sid(1)], fail_upload=[sid(2)])
        self.assertEqual(json.loads(self.path.read_text())["tracks"],
                         {sid(1): {"status": "no_match", "retry_after": "2026-10-15"}})

    def test_state_file_holds_ids_and_dates_only(self):
        self.go([song(1)], self.state(), no_match=[sid(1)])
        text = self.path.read_text()
        self.assertNotIn("T1", text)  # no title, album, artist or url
        self.assertEqual(set(json.loads(text)["tracks"][sid(1)]), {"status", "retry_after"})

    def test_tracks_in_cooldown_are_skipped_and_do_not_use_the_cap(self):
        st = self.state()
        self.go([song(1), song(2)], st, no_match=[sid(1)], fail_upload=[sid(2)])
        limits = ingest.RunLimits(max_new=1)
        searched = []
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            f = Fakes(Path(d))
            deps = f.deps()
            deps.find_match = lambda s: searched.append(s["song_id"]) or object()
            c = ingest.run([song(1), song(2), song(3), song(4)], deps, set(), {}, Path(d), False, limits, st)
        self.assertEqual(searched, [sid(3)])  # 1 and 2 never searched or downloaded
        self.assertEqual((c["cooldown"], c["new"], limits.stop_reason, limits.not_attempted), (2, 1, "cap", 1))
        self.assertIn("skipped (cooldown, retry later): A - T1", out.getvalue())
        self.assertNotIn(sid(1), out.getvalue())  # ids are not printed

    def test_cooldown_ends_on_the_retry_date_and_success_clears_the_entry(self):
        st = self.state()
        self.go([song(1)], st, no_match=[sid(1)])
        self.today.day = ingest.datetime.date(2026, 11, 8)
        self.assertTrue(st.in_cooldown(sid(1)))
        self.today.day = ingest.datetime.date(2026, 11, 9)
        self.assertFalse(st.in_cooldown(sid(1)))
        f, c, _ = self.go([song(1)], st)  # retried: now it matches
        self.assertEqual((c["new"], c["cooldown"], st.entries), (1, 0, {}))
        self.assertEqual(json.loads(self.path.read_text())["tracks"], {})

    def test_a_track_already_in_the_library_is_not_counted_as_cooldown(self):
        st = self.state()
        self.go([song(1)], st, fail_upload=[sid(1)])
        _, c, _ = self.go([song(1)], st, existing=[sid(1)])
        self.assertEqual((c["exists"], c["cooldown"], st.entries), (1, 0, {}))

    def test_state_survives_a_restart_and_drops_expired_entries(self):
        self.go([song(1), song(2)], self.state(), no_match=[sid(1)], fail_upload=[sid(2)])
        again = self.state()  # a new process reads the file
        self.assertEqual(set(again.entries), {sid(1), sid(2)})
        self.today.day = ingest.datetime.date(2026, 10, 14)  # failed (3 days) expired, no_match not
        later = self.state()
        self.assertEqual(set(later.entries), {sid(1)})

    def test_dry_run_reads_the_state_but_never_writes_it(self):
        st = self.state()
        self.go([song(1)], st, dry=True, no_match=[sid(1)])
        self.assertFalse(self.path.exists())

    def test_sign_in_challenge_is_not_recorded_as_a_track_failure(self):
        st = self.state()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            f = Fakes(Path(d))
            deps = f.deps()

            def blocked(s, m, tdir):
                raise ingest.YouTubeSignInRequired("x")

            deps.download = blocked
            with self.assertRaises(ingest.YouTubeSignInRequired):
                ingest.run([song(1)], deps, set(), {}, Path(d), False, None, st)
        self.assertEqual(st.entries, {})
        self.assertFalse(self.path.exists())

    def test_file_is_private_and_atomic(self):
        self.go([song(1)], self.state(), no_match=[sid(1)])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual([x.name for x in self.path.parent.iterdir()], [self.path.name])  # no temp file left

    def test_failed_write_keeps_the_previous_file_and_warns_once(self):
        st = self.state()
        self.go([song(1)], st, no_match=[sid(1)])
        before = self.path.read_text()
        with mock.patch("os.replace", side_effect=OSError("boom")):
            _, _, out = self.go([song(2), song(3)], st, no_match=[sid(2), sid(3)])
        self.assertEqual(self.path.read_text(), before)  # old content intact
        self.assertEqual([x.name for x in self.path.parent.iterdir()], [self.path.name])  # temp file removed
        self.assertEqual(out.count("WARN could not write the state file"), 1)
        self.assertTrue(st.in_cooldown(sid(2)))  # still applies to the rest of this run

    def test_write_interrupted_midway_keeps_the_previous_file(self):
        self.go([song(1)], self.state(), no_match=[sid(1)])
        before = self.path.read_text()
        with mock.patch("json.dump", side_effect=RuntimeError("killed")), self.assertRaises(RuntimeError):
            ingest.atomic_write_json(self.path, {"x": 1})
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual([x.name for x in self.path.parent.iterdir()], [self.path.name])

    def test_corrupted_files_start_empty_with_a_warning_and_never_crash(self):
        self.path.parent.mkdir(parents=True)
        for name, raw in (("garbage", b"\x00\xff{{{"), ("empty", b""), ("truncated", b'{"version": 1, "tracks": {"'),
                          ("not an object", b"[1, 2]"), ("wrong version", b'{"version": 2, "tracks": {}}'),
                          ("tracks not an object", b'{"version": 1, "tracks": []}'), ("deep", b"[" * 100000)):
            with self.subTest(name):
                self.path.write_bytes(raw)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    st = self.state()
                self.assertEqual(st.entries, {})
                self.assertIn("WARN state file is corrupted", out.getvalue())
        # the next change replaces the corrupted file with a valid one
        self.go([song(1)], st, no_match=[sid(1)])
        self.assertIn(sid(1), json.loads(self.path.read_text())["tracks"])

    def test_malformed_entries_are_ignored_and_good_ones_kept(self):
        self.path.parent.mkdir(parents=True)
        good = {"status": "failed", "retry_after": "2026-10-12"}
        self.path.write_text(json.dumps({"version": 1, "tracks": {
            sid(1): good, "../x": good, sid(2): {"status": "bogus", "retry_after": "2026-10-12"},
            sid(3): {"status": "failed", "retry_after": "not a date"}, sid(4): "x", sid(5): {}}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st = self.state()
        self.assertEqual(set(st.entries), {sid(1)})
        self.assertIn("ignored 5 malformed entries", out.getvalue())

    def test_unreadable_state_path_is_not_fatal(self):
        self.path.mkdir(parents=True)  # a directory where the file should be
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            st = self.state()
        self.assertEqual(st.entries, {})
        self.assertIn("WARN state file unreadable", out.getvalue())

    def test_default_path_is_next_to_the_log(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/x/state"}):
            self.assertEqual(ingest.default_state_path(), Path("/x/state/cowlic/ingest-state.json"))
        env = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("os.path.expanduser", return_value="/h"):
            self.assertEqual(ingest.default_state_path(), Path("/h/.local/state/cowlic/ingest-state.json"))


class StopReasonMainTest(unittest.TestCase):
    """main(): options reach run_playlists, the stop reason lands in the log and in --result-json."""

    def run_main(self, argv, run_playlists):
        env = {k: "x" for k in ingest.REQUIRED_ENV}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("tg_check.load_env"), \
                mock.patch.object(sys, "argv", ["ingest.py", *argv]), \
                mock.patch.object(ingest, "d1_query", return_value=[]), \
                mock.patch.object(ingest, "js_runtime_problem", return_value=None), \
                mock.patch.object(ingest, "real_deps", return_value=None), \
                mock.patch.dict(sys.modules, {"ytmusicapi": SimpleNamespace(YTMusic=lambda: None)}), \
                mock.patch.object(ingest, "run_playlists", run_playlists), \
                contextlib.redirect_stdout(out):
            return ingest.main(), out.getvalue()

    OK = {"new": 1, "exists": 0, "no_match": 0, "too_big": 0, "failed": 0}

    def reason(self, run_playlists, extra=()):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            code, out = self.run_main([GOOD_URL, "--result-json", str(p), "--state-file", str(Path(d) / "s.json"),
                                       *extra], run_playlists)
            return code, out, json.loads(p.read_text())

    def test_done(self):
        code, out, r = self.reason(lambda urls, *a: {u: dict(self.OK) for u in urls})
        self.assertEqual((code, r["stop_reason"], r["failure_class"]), (0, "done", "none"))
        self.assertIn("stop reason: done", out)

    def test_options_are_passed_on_and_a_cap_stop_is_reported(self):
        seen = {}

        def capped(urls, fetch, deps, existing, covers, work, dry, limits, state):
            seen.update(max_new=limits.max_new, minutes=limits.deadline is not None, state=state.path.name,
                        days=state.retry_days)
            limits.stop("cap", 4)
            limits.stopped_url = urls[0]
            return {u: dict(self.OK) for u in urls}

        code, out, r = self.reason(capped, ["--max-new", "1", "--max-minutes", "5", "--retry-no-match-days", "9",
                                            "--retry-failed-days", "1"])
        self.assertEqual(seen, {"max_new": 1, "minutes": True, "state": "s.json", "days": {"no_match": 9, "failed": 1}})
        self.assertEqual((code, r["stop_reason"], r["failure_class"]), (0, "cap", "none"))
        self.assertEqual(r["playlists"][0]["status"], "stopped")
        self.assertIn("stop reason: cap", out)

    def test_time_stop_with_a_failure_still_exits_1(self):
        def timed(urls, fetch, deps, existing, covers, work, dry, limits, state):
            limits.stop("time")
            return {u: dict(self.OK, failed=1) for u in urls}

        code, out, r = self.reason(timed)
        self.assertEqual((code, r["stop_reason"], r["failure_class"]), (1, "time", "partial"))

    def test_sign_in(self):
        def sign_in(urls, *a):
            e = ingest.YouTubeSignInRequired("x")
            e.counts, e.results = dict(self.OK), {}
            raise e

        code, out, r = self.reason(sign_in)
        self.assertEqual((code, r["stop_reason"]), (3, "sign-in"))
        self.assertIn("stop reason: sign-in", out)

    def test_nothing_ran_has_no_stop_reason(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            code, out = self.run_main(["https://example.invalid/x", "--result-json", str(p)], None)
            self.assertEqual(code, 1)
            self.assertIsNone(json.loads(p.read_text())["stop_reason"])
            self.assertNotIn("stop reason", out)

    def test_existing_result_keys_are_unchanged(self):
        _, _, r = self.reason(lambda urls, *a: {u: dict(self.OK) for u in urls})
        self.assertLessEqual({"failure_class", "exit_code", "playlists", "counts"}, set(r))
        self.assertLessEqual({"url", "status", "added", "skipped", "no_match", "too_big", "failed", "not_attempted"},
                             set(r["playlists"][0]))

    def test_existing_run_has_no_limits_by_default(self):
        seen = {}

        def look(urls, fetch, deps, existing, covers, work, dry, limits, state):
            seen.update(max_new=limits.max_new, deadline=limits.deadline)
            return {u: dict(self.OK) for u in urls}

        self.reason(look)
        self.assertEqual(seen, {"max_new": None, "deadline": None})


if __name__ == "__main__":
    unittest.main()

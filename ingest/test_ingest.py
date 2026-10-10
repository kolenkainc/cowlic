import contextlib
import io
import os
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
        out = download_log(self.work, "block")
        self.assertIn("expected exactly one mp3 after download", out)
        self.assertIn("Sign in to confirm you're not a bot", out)

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
        msg = "ERROR: [youtube] x: Sign in to confirm you're not a bot"
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
                    f"{GOOD_URL}", "key: value", f"-{GOOD_URL}"):
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
        with self.assertRaises(ingest.IngestError):
            ingest.validate_playlist_url("https://example.invalid/playlist")


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


if __name__ == "__main__":
    unittest.main()

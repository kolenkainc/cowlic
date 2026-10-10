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
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ingest.yml"

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

    def test_long_lines_are_capped(self):
        self.assertEqual(len(ingest.tail_of("x" * 5000, [])), ingest.LINE_MAX)

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


def step_lines(name: str) -> list[str]:
    """Lines of one workflow step (from its `- name:` line up to the next step), from the real workflow file."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"- name: {name}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= indent), len(lines))
    return lines[start:end]


def summary_script() -> str:
    """Body of the Summary step's `run: |` block, taken from the real workflow file."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "- name: Summary")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for ln in lines[run + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        body.append(ln)
    return "\n".join(body)


@unittest.skipUnless(shutil.which("bash"), "bash needed")
class SummaryStepTest(unittest.TestCase):
    def summary(self, log_text: str) -> str:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ingest.log").write_text(log_text)
            out = Path(d) / "summary.md"
            subprocess.run(["bash", "-c", summary_script()], check=True,
                           env={"PATH": os.environ["PATH"], "RUNNER_TEMP": d, "GITHUB_STEP_SUMMARY": str(out),
                                "JOB_STATUS": "failure"})
            return out.read_text()

    def test_block_message_from_surfaced_output_is_flagged(self):
        case = SpotdlOutputTest("test_exit_zero_without_file_surfaces_block_message")
        case.setUp()
        try:
            log_text = download_log(case.work, "block")
        finally:
            case.doCleanups()
        self.assertIn("YouTube appears to block", self.summary(log_text))

    def test_telegram_429_is_not_a_youtube_block(self):
        text = self.summary("ERROR A - T: IngestError: 429: Too Many Requests: retry after 5\n")
        self.assertNotIn("YouTube appears to block", text)

    def test_backtick_lines_cannot_break_out_of_the_block(self):
        text = self.summary("ERROR x\n````\n<script>alert(1)</script>\n# heading\n")
        for needle in ("````", "<script>", "# heading"):
            hits = [ln for ln in text.splitlines() if needle in ln]
            self.assertTrue(hits)
            for ln in hits:
                self.assertTrue(ln.startswith("    "), ln)

    BLOCK = "yt_dlp.utils.DownloadError: ERROR: [youtube] x: Sign in to confirm you're not a bot"
    FAILED = "ERROR A - T: IngestError: expected exactly one mp3 after download; spotdl output (last lines):"
    SPOTDL_DENO_WARNING = "Some YouTube downloads require Deno. Run spotdl --download-deno or install Deno"

    def test_missing_runtime_is_reported_from_ingest_warning(self):
        text = self.summary(f"WARN js runtime: deno not found on PATH; yt-dlp needs Deno\n{self.FAILED}\n")
        self.assertIn("No usable JavaScript runtime", text)
        self.assertNotIn("YouTube appears to block", text)
        self.assertNotIn("another reason", text)

    def test_missing_runtime_is_reported_from_yt_dlp_error(self):
        for marker in ("n challenge solving failed: Some formats may be missing", "Signature solving failed",
                       "No supported JavaScript runtime could be found"):
            with self.subTest(marker):
                text = self.summary(f"{self.FAILED}\nERROR: {marker}\n")
                self.assertIn("No usable JavaScript runtime", text)

    def test_block_and_runtime_messages_are_distinct(self):
        block = self.summary(f"{self.FAILED}\n{self.BLOCK}\n")
        self.assertIn("YouTube appears to block", block)
        self.assertNotIn("No usable JavaScript runtime", block)
        self.assertNotIn("another reason", block)

    def test_other_download_error_has_its_own_message(self):
        text = self.summary(f"{self.FAILED}\n[x] ERROR: [youtube] x: Video unavailable. This video is private\n")
        self.assertIn("another reason", text)
        self.assertNotIn("YouTube appears to block", text)
        self.assertNotIn("No usable JavaScript runtime", text)

    def test_spotdl_deno_warning_alone_is_not_a_missing_runtime(self):
        # spotdl prints it for ANY failed download (the first dispatch failed with it for another cause)
        text = self.summary(f"{self.FAILED}\n{self.SPOTDL_DENO_WARNING}\nAudioProviderError: YT-DLP download error\n")
        self.assertNotIn("No usable JavaScript runtime", text)
        self.assertIn("another reason", text)

    def test_no_cause_message_without_download_failures(self):
        text = self.summary("ingested: A - T\ndone: 1 new, 0 already present, 0 no match, 0 too big, 0 failed\n")
        for needle in ("YouTube appears to block", "No usable JavaScript runtime", "another reason"):
            self.assertNotIn(needle, text)


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


class InstallDenoStepTest(unittest.TestCase):
    def setUp(self):
        self.step = "\n".join(step_lines("Install Deno"))

    def test_runs_before_ingest_and_has_no_secrets(self):
        text = WORKFLOW.read_text()
        self.assertLess(text.index("- name: Install Deno"), text.index("- name: Run ingest"))
        self.assertNotIn("secrets.", self.step)
        self.assertNotIn("TG_", self.step)

    def test_release_is_pinned_and_checksum_verified(self):
        self.assertRegex(self.step, r"DENO_VERSION=\d+\.\d+\.\d+\n")
        self.assertRegex(self.step, r"DENO_SHA256=[0-9a-f]{64}\n")
        self.assertIn("sha256sum --check --strict", self.step)
        self.assertLess(self.step.index("sha256sum --check"), self.step.index("unzip"))
        self.assertIn("set -euo pipefail", self.step)
        self.assertNotIn("latest", self.step)

    def test_pinned_version_meets_yt_dlp_minimum(self):
        m = re.search(r"DENO_VERSION=(\d+)\.(\d+)\.(\d+)", self.step)
        self.assertGreaterEqual(tuple(int(x) for x in m.groups()), ingest.MIN_DENO)


if __name__ == "__main__":
    unittest.main()

"""Private failed-download evidence through the real quiet-track boundary. No network."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import ingest
import tg_check

SID = "lfm:0123456789abcdef"
VID = "abcdefghijk"
SONG = {"song_id": SID, "name": "Private Title", "artists": ["Private Artist"],
        "album_artist": "Private Artist", "album_name": "Private Album", "duration": 200,
        "year": 2026, "track_number": 1, "disc_number": 1, "url": None, "cover_url": None}


class DownloadDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = ingest.RunState(self.root / "state.json")
        self.reports = self.root / "download-failures"

    def run_track(self, error=None, upload_error=None):
        self.uploads = 0
        def download(song, match, directory):
            if error:
                raise error
            path = directory / "audio.mp3"
            path.write_bytes(b"synthetic mp3")
            return path
        def upload(song, path):
            self.uploads += 1
            if upload_error:
                raise upload_error
            return {"audio": {"file_id": "synthetic"}, "message_id": 1}
        deps = ingest.Deps(lambda _: SimpleNamespace(video_id=VID), download, upload,
                           lambda *_: "cover", lambda _: [], lambda _: None)
        output, outcomes = io.StringIO(), {}
        with contextlib.redirect_stdout(output):
            counts = ingest.run([SONG], deps, set(), {}, self.root, False,
                                state=self.state, outcomes=outcomes, quiet=True)
        return counts, outcomes, output.getvalue()

    def test_failed_download_preserves_private_correlatable_redacted_evidence(self):
        error = ingest.IngestError("yt-dlp failed with exit code 1; Private Title\n"
                                  "ERROR: HTTP Error 403 BOT-CANARY CUSTOM-CANARY "
                                  "https://media.invalid/audio?sig=SECRET-SIGNATURE&ip=1.2.3.4")
        with mock.patch.dict(os.environ, {"TG_BOT_TOKEN": "BOT-CANARY", "CUSTOM_SECRET": "CUSTOM-CANARY"}), \
             mock.patch.object(tg_check, "ENV_FROM_FILE", {"CUSTOM_SECRET"}):
            counts, outcomes, output = self.run_track(error)
        self.assertEqual((counts["failed"], outcomes, self.uploads), (1, {SID: "failed"}, 0))
        self.assertEqual(self.state.entries[SID]["status"], "failed")
        (path,) = self.reports.glob("*.json")
        self.assertEqual(path.name, "download-" + hashlib.sha256(SID.encode()).hexdigest() + ".json")
        report = json.loads(path.read_text())
        self.assertEqual((report["song_id"], report["video_id"]), (SID, VID))
        self.assertIn("HTTP Error 403", report["detail"])
        self.assertIn("Private Title", report["detail"])
        for private in ("BOT-CANARY", "CUSTOM-CANARY", "SECRET-SIGNATURE", "1.2.3.4"):
            self.assertNotIn(private, path.read_text())
        for private in ("Private Title", "Private Artist", VID, "HTTP Error 403", "BOT-CANARY", "CUSTOM-CANARY"):
            self.assertNotIn(private, output)
        self.assertEqual(stat.S_IMODE(self.reports.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_success_and_later_upload_failure_do_not_become_download_reports(self):
        self.assertEqual(self.run_track()[0]["new"], 1)
        self.assertFalse(self.reports.exists())
        self.assertEqual(self.run_track(upload_error=ingest.IngestError("Telegram API error 429: flood"))[0]["failed"], 1)
        self.assertFalse(self.reports.exists())

    def test_report_failure_does_not_change_original_outcome_or_cooldown(self):
        with mock.patch.object(ingest.tempfile, "mkstemp", side_effect=OSError("PRIVATE-CANARY")):
            counts, outcomes, output = self.run_track(ingest.IngestError("yt-dlp failed with exit code 1"))
        self.assertEqual((counts["failed"], outcomes, self.state.entries[SID]["status"]), (1, {SID:"failed"}, "failed"))
        self.assertIn("private download diagnostic unavailable (OSError)", output)
        self.assertNotIn("PRIVATE-CANARY", output)
        self.assertFalse(list(self.reports.glob("*.json")))

    def test_bounded_retention_preserves_unrelated_files_and_replaces_one_track_atomically(self):
        self.reports.mkdir(mode=0o700)
        unrelated = self.reports / "notes.json"
        unrelated.write_text("keep")
        error = ingest.IngestError("yt-dlp failed with exit code 1\n" + ("private detail " * 50 + "\n") * 200)
        for number in range(12):
            sid = f"lfm:{number:016x}"
            self.assertTrue(ingest.save_download_failure(self.state.path, sid, VID, error))
        reports = list(self.reports.glob("download-*.json"))
        self.assertEqual(len(reports), 10)
        for path in reports:
            self.assertLessEqual(path.stat().st_size, 65536)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(ingest.save_download_failure(self.state.path, sid, VID, ingest.IngestError("updated")))
        self.assertEqual(len(list(self.reports.glob("download-*.json"))), 10)
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertFalse(list(self.reports.glob(".pending-*")))

    def test_symlink_public_checkout_and_open_directory_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.reports.symlink_to(outside, target_is_directory=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ingest.save_download_failure(self.state.path, SID, VID, ValueError("private")))
        self.assertEqual(list(outside.iterdir()), [])
        self.reports.unlink()
        self.reports.mkdir(mode=0o755)
        self.reports.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ingest.save_download_failure(self.state.path, SID, VID, ValueError("private")))
            public = Path(ingest.__file__).resolve().parent / "state.json"
            self.assertFalse(ingest.save_download_failure(public, SID, VID, ValueError("private")))
        self.assertEqual(list(self.reports.iterdir()), [])
        self.reports.chmod(0o700)
        target = outside / "keep.txt"
        target.write_text("keep")
        report = self.reports / ("download-" + hashlib.sha256(SID.encode()).hexdigest() + ".json")
        report.symlink_to(target)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(ingest.save_download_failure(self.state.path, SID, VID, ValueError("private")))
        self.assertTrue(report.is_symlink())
        self.assertEqual(target.read_text(), "keep")

    def test_sign_in_stop_is_not_retried_or_downgraded_to_a_track_failure(self):
        with self.assertRaises(ingest.YouTubeSignInRequired):
            self.run_track(ingest.YouTubeSignInRequired("YouTube asks to sign in"))
        self.assertFalse(self.reports.exists())
        self.assertEqual(self.state.entries, {})
        self.assertEqual(self.uploads, 0)


if __name__ == "__main__":
    unittest.main()

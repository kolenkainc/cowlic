"""Offline refusal and identity tests for the bounded research command."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error

import artwork_probe as probe

ROW = {"album": "A Record", "artist": "Test Artist", "title": "Opening Song", "duration_s": 200}
RELEASE = {"id": "01234567-0123-0123-0123-012345678901", "title": "A Record",
           "artist-credit": [{"artist": {"name": "Test Artist"}}],
           "media": [{"tracks": [{"title": "Opening Song", "length": 200000}]}]}


class Reply(io.BytesIO):
    def __init__(self, data, content_type="application/json"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}


class ProbeTests(unittest.TestCase):
    def test_refuses_host_spoofing_and_redirects(self):
        for url in ("http://musicbrainz.org/x", "https://musicbrainz.org.evil/x", "https://evil@musicbrainz.org/x",
                    "https://musicbrainz.org:444/x", "https://musicbrainz.org/x#fragment"):
            with self.subTest(url=url), self.assertRaises(probe.Refusal):
                probe.safe_url(url, {probe.MB})
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("safe", 307, "redirect", {}, None)
        with self.assertRaisesRegex(probe.Refusal, "http_307"):
            probe.Source(opener).get("https://musicbrainz.org/x", {probe.MB})

    def test_stop_throttling_not_retry(self):
        for code in (429, 503):
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError("safe", code, "busy", {}, None)
            with self.assertRaises(probe.Unavailable):
                probe.Source(opener).get("https://musicbrainz.org/x", {probe.MB})
            self.assertEqual(opener.open.call_count, 1)

    def test_budget_pacing_and_user_agent(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: Reply(b"{}")
        clock, sleep = Mock(return_value=100), Mock()
        source = probe.Source(opener, clock, sleep)
        source.get("https://musicbrainz.org/x", {probe.MB})
        source.get("https://musicbrainz.org/y", {probe.MB})
        sleep.assert_called_once_with(1.5)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), probe.UA)
        source.requests = 25
        with self.assertRaises(probe.Unavailable):
            source.get("https://musicbrainz.org/x", {probe.MB})
        self.assertEqual(opener.open.call_count, 2)

    def test_bounds_types_and_magic(self):
        for data, ctype, image in ((b"x" * (probe.MAX_JSON + 1), "application/json", False),
                                  (b"[]", "application/json", False), (b"{}", "text/html", False),
                                  (b"fake", "image/jpeg", True), (b"\xff\xd8jpeg", "text/plain", True)):
            opener = Mock()
            opener.open.return_value = Reply(data, ctype)
            with self.subTest(ctype=ctype, image=image), self.assertRaises(probe.Refusal):
                probe.Source(opener).get("https://archive.org/x", {"archive.org"}, image=image)

    def test_identity_rejects_edition_artist_duration_and_track_variants(self):
        self.assertTrue(probe.release_ok(ROW, RELEASE, tracks=True))
        for row in ({**ROW, "album": "A Record Deluxe"}, {**ROW, "artist": "Different Artist"},
                    {**ROW, "title": "Opening Song Live"}, {**ROW, "duration_s": 240}):
            with self.subTest(row=row):
                self.assertFalse(probe.release_ok(row, RELEASE, tracks=True))

    def test_end_to_end_private_evidence_with_real_guards(self):
        source = Mock()
        source.get.side_effect = [{"releases": [RELEASE]}, RELEASE,
                                  {"images": [{"front": True, "thumbnails": {"small": "https://archive.org/x.jpg"}}]},
                                  b"\xff\xd8verified"]
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(probe.probe_one(ROW, source, Path(temp)), "verified_release_and_jpeg")
            self.assertEqual((Path(temp) / "front.jpg").read_bytes(), b"\xff\xd8verified")
            self.assertTrue((Path(temp) / "release-0.json").exists())
        self.assertEqual(source.get.call_count, 4)

    def test_rejected_tracks_never_reach_artwork(self):
        source = Mock()
        page = {**RELEASE, "media": []}
        source.get.side_effect = [{"releases": [RELEASE]}, page]
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(probe.probe_one(ROW, source, Path(temp)), "no_guarded_release")
        self.assertEqual(source.get.call_count, 2)

    def test_input_cap_and_types(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            for rows in ([], [ROW] * 6, [{**ROW, "duration_s": True}], [{**ROW, "album": ""}]):
                path.write_text(json.dumps(rows))
                with self.assertRaises(probe.Refusal):
                    probe.load_rows(path)
            path.write_text(json.dumps([ROW] * 5))
            self.assertEqual(len(probe.load_rows(path)), 5)


if __name__ == "__main__":
    unittest.main()

"""Tests for ship_grafana: a fake Loki endpoint on 127.0.0.1 (in-process), temp files, no network, no Grafana.
validate_url is patched to point at the fake server only where a request must actually be sent; its own rules
are tested directly, and main() with a foreign host must send nothing."""
import base64
import calendar
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import ship_grafana as sg

CANARY_TOKEN = "glc_canaryTokenABCDEFGHIJKLMNOP0123456789"
CANARY_USER = "1234567"
CANARY_LINE_SECRET = "canaryLineSecret-ZZZZ9999"  # a secret ingest knows; only the redaction hook removes it here
HOST = "grafana.example.grafana.net"


class Fake:
    """Fake Loki. mode: 'ok' (204), an int status, 'hang', 'redirect'."""

    def __init__(self, mode="ok"):
        self.mode, self.requests, self.stop = mode, [], threading.Event()
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n)})
                if fake.mode == "hang":
                    fake.stop.wait(10)
                    return
                if fake.mode == "redirect":
                    self.send_response(302)  # urllib follows a 302 of a POST as a GET unless told not to
                    self.send_header("Location", f"http://127.0.0.1:{fake.port}/elsewhere")
                    self.end_headers()
                    return
                self.send_response(204 if fake.mode == "ok" else fake.mode)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.base}/loki/api/v1/push"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()


def fake_redact(text, secrets):  # same contract as ingest.redact, to keep the unit tests independent
    import ingest
    return ingest.redact(text, secrets)


LAST_RUN = {"started_at": "2026-10-10T19:10:01Z", "finished_at": "2026-10-10T19:12:40Z", "exit_code": 0,
            "failure_class": "none", "log_file": "ingest.log",
            "result": {"playlists": [{"url": "https://open.spotify.com/playlist/AAAA"}],
                       "counts": {"added": 2, "skipped": 1, "no_match": 0, "too_big": 0, "failed": 0,
                                  "not_attempted": 0, "playlists_unreadable": 0, "playlists_not_attempted": 0}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name, "ingest.log")
        self.lr = Path(self.tmp.name, "last-run.json")
        self.lr.write_text(json.dumps(LAST_RUN))
        self.servers = []
        self.addCleanup(lambda: [s.close() for s in self.servers])

    def fake(self, mode="ok"):
        f = Fake(mode)
        self.servers.append(f)
        return f

    def env(self, fake=None):
        return {"INGEST_GRAFANA_LOKI_URL": f"https://{HOST}", "INGEST_GRAFANA_LOKI_USER": CANARY_USER,
                "INGEST_GRAFANA_TOKEN": CANARY_TOKEN}

    def run_ship(self, fake, offset=0, env=None, **patches):
        """ship() against the fake; returns captured stdout."""
        out = io.StringIO()
        point = lambda raw, path=sg.PUSH_PATH: fake.base + path  # noqa: E731 - each push goes to its own path
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", side_effect=point):
            with mock.patch.multiple(sg, MAX_LINES=sg.MAX_LINES, **patches):
                sg.ship(str(self.log), offset, str(self.lr), env=env or self.env(), redact=fake_redact)
        return out.getvalue()


class PayloadTests(Base):
    def test_payload_format_and_labels(self):
        self.log.write_text("old line before the run\n2026 run-ingest-local: start\nplain ingest line\n"
                            "2026 run-ingest-local: WARN prerequisites: deno\nERROR boom\n"
                            "2026 run-ingest-local: finished: exit code 0, ok\n")
        offset = len("old line before the run\n")
        f = self.fake()
        out = self.run_ship(f, offset)
        self.assertEqual(len(f.requests), 1)
        r = f.requests[0]
        self.assertEqual(r["path"], "/loki/api/v1/push")
        self.assertEqual(r["headers"]["Content-Type"], "application/json")
        body = json.loads(r["body"])
        levels = {}
        for s in body["streams"]:
            self.assertEqual(set(s["stream"]), {"job", "host", "level"})
            self.assertEqual(s["stream"]["job"], "cowlic-ingest")
            levels[s["stream"]["level"]] = s["values"]
            for ts, line in s["values"]:
                self.assertIsInstance(ts, str)  # a number gives HTTP 400 on Loki
                self.assertTrue(ts.isdigit())
                self.assertIsInstance(line, str)
        self.assertEqual(set(levels), {"info", "warn", "error"})
        all_lines = [v[1] for vals in levels.values() for v in vals]
        self.assertNotIn("old line before the run", "\n".join(all_lines))
        self.assertIn("plain ingest line", "\n".join(all_lines))
        self.assertTrue(any("WARN prerequisites" in v[1] for v in levels["warn"]))
        self.assertTrue(any(v[1] == "ERROR boom" for v in levels["error"]))
        ts = sorted(int(v[0]) for vals in levels.values() for v in vals)
        self.assertEqual(len(ts), len(set(ts)))
        self.assertEqual(out.strip(), "grafana: loki push ok (HTTP 204)")

    def test_run_summary_line(self):
        self.log.write_text("a line\n")
        f = self.fake()
        self.run_ship(f)
        body = json.loads(f.requests[0]["body"])
        lines = [v[1] for s in body["streams"] for v in s["values"]]
        self.assertIn("run_summary exit_code=0 failure_class=none added=2 skipped=1 no_match=0 too_big=0 "
                      "failed=0 not_attempted=0 duration_s=159", lines)
        self.assertEqual(len([ln for ln in lines if ln.startswith("run_summary")]), 1)
        self.assertNotIn("playlist", json.dumps(body))  # URLs and playlist rows are not shipped

    def test_failed_run_summary_goes_to_error_stream(self):
        self.lr.write_text(json.dumps({**LAST_RUN, "exit_code": 1, "failure_class": "partial"}))
        self.log.write_text("x\n")
        f = self.fake()
        self.run_ship(f)
        body = json.loads(f.requests[0]["body"])
        err = [s for s in body["streams"] if s["stream"]["level"] == "error"][0]
        self.assertTrue(err["values"][0][1].startswith("run_summary exit_code=1 failure_class=partial"))

    def test_basic_auth_present_never_printed(self):
        self.log.write_text("x\n")
        f = self.fake()
        out = self.run_ship(f)
        expect = "Basic " + base64.b64encode(f"{CANARY_USER}:{CANARY_TOKEN}".encode()).decode()
        self.assertEqual(f.requests[0]["headers"]["Authorization"], expect)
        for secret in (CANARY_TOKEN, CANARY_USER, expect.split()[1], HOST, "127.0.0.1"):
            self.assertNotIn(secret, out)

    def test_canary_never_in_payload(self):
        self.log.write_text(f"line with {CANARY_TOKEN} inside\nwrapped {CANARY_TOKEN[:20]}\n{CANARY_TOKEN[20:]} end\n"
                            f"b64 {base64.b64encode(f'{CANARY_USER}:{CANARY_TOKEN}'.encode()).decode()}\n"
                            f"\x1b[31mcolored {CANARY_TOKEN}\x1b[0m\nfine line\n")
        f = self.fake()
        self.run_ship(f)
        raw = f.requests[0]["body"].decode()
        self.assertNotIn(CANARY_TOKEN, raw)
        self.assertNotIn(CANARY_TOKEN[:20], raw)
        self.assertNotIn(base64.b64encode(f"{CANARY_USER}:{CANARY_TOKEN}".encode()).decode(), raw)
        self.assertIn("fine line", raw)

    def test_unsafe_chars_between_token_characters_do_not_rebuild_it(self):
        # control/format characters must be stripped BEFORE redaction, or stripping rebuilds the plain token
        for sep in ("\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1b", "\x1f", "\x7f", "\x85", "​", "⁠",
                    "﻿", " ", "­", "​\x01"):
            with self.subTest(sep=repr(sep)):
                self.log.write_text(f"leak {sep.join(CANARY_TOKEN)} end\nok line\n")
                f = self.fake()
                self.run_ship(f)
                raw = f.requests[-1]["body"].decode()
                self.assertNotIn(CANARY_TOKEN, raw)
                self.assertNotIn(CANARY_TOKEN, json.loads(raw)["streams"][0]["values"][0][1].replace(sep, ""))
                lines = [v[1] for s in json.loads(raw)["streams"] for v in s["values"]]
                self.assertFalse(any(CANARY_TOKEN in ln for ln in lines))
                self.assertTrue(all(c in "\n\t" or ord(c) >= 0x20 and ord(c) != 0x7f for ln in lines for c in ln))

    def test_base64_credential_with_separators(self):
        b64 = base64.b64encode(f"{CANARY_USER}:{CANARY_TOKEN}".encode()).decode()
        self.log.write_text(f"x {chr(1).join(b64)} y\n")
        f = self.fake()
        self.run_ship(f)
        self.assertNotIn(b64, f.requests[0]["body"].decode())

    def test_ansi_sequences_removed_whole(self):
        self.assertEqual(sg.sanitize_lines("\x1b[31mred\x1b[0m and \x1b]0;title\x07x\n", [CANARY_TOKEN], fake_redact),
                         ["red and x"])

    def test_strip_unsafe_keeps_newline_and_tab(self):
        self.assertEqual(sg.strip_unsafe("a\x00b\tc\nd​e\x1b[f"), "ab\tc\nde[f")

    def test_summary_goes_through_cleaning_and_final_guard(self):
        self.log.write_text("x\n")
        f = self.fake()
        seen = []

        def spy(text, secrets):
            seen.append(text)
            return fake_redact(text, secrets)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", return_value=f.url):
            sg.ship(str(self.log), 0, str(self.lr), env=self.env(), redact=spy)
        self.assertTrue(any(t.startswith("run_summary") for t in seen))
        # a summary that somehow carries the token is never sent
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sg, "validate_url", return_value=f.url), \
                mock.patch.object(sg, "summary_line", return_value=f"run_summary {CANARY_TOKEN}"), \
                mock.patch.object(sg, "clean_text", side_effect=lambda t, s, r: t):
            n = len(f.requests)
            with self.assertRaises(sg.ShipError):
                sg.ship(str(self.log), 0, str(self.lr), env=self.env(), redact=fake_redact)
            self.assertEqual(len(f.requests), n)

    def test_ingest_redact_is_the_real_one(self):
        # end to end with the real ingest.redact through the default import (no redact= override)
        self.log.write_text(f"leak {CANARY_TOKEN}\n")
        f = self.fake()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", return_value=f.url):
            sg.ship(str(self.log), 0, str(self.lr), env=self.env())
        self.assertNotIn(CANARY_TOKEN, f.requests[0]["body"].decode())

    def test_control_chars_long_lines_and_cap(self):
        long = "x" * 10000
        many = "\n".join(f"line {i}" for i in range(3000))
        self.log.write_text(f"bell\x07 and nul\x00 done\n{long}\n{many}\n")
        f = self.fake()
        self.run_ship(f)
        body = json.loads(f.requests[0]["body"])
        lines = [v[1] for s in body["streams"] for v in s["values"]]
        self.assertIn("bell and nul done", lines)
        self.assertTrue(all(len(ln) <= sg.MAX_LINE_CHARS + 20 for ln in lines))
        self.assertLessEqual(len(lines), sg.MAX_LINES + 1)  # + run_summary
        self.assertTrue(any("lines omitted" in ln for ln in lines))
        self.assertIn("line 2999", lines)  # the tail (finish line) survives the cap


PROM_USER = "7654321"
FIELD_NAMES = {"last_run_timestamp_seconds", "success", "exit_code", "duration_seconds", "added", "skipped",
               "no_match", "too_big", "failed", "not_attempted", "failure_partial", "failure_config",
               "failure_youtube_sign_in", "failure_crash"}


def parse_influx(body: bytes):
    """measurement, tags, fields of a one-line Influx payload (no escaping is expected in ours)."""
    text = body.decode()
    assert text.endswith("\n") and text.count("\n") == 1, text
    head, fields = text.strip().split(" ")
    meas, *tags = head.split(",")
    return meas, dict(t.split("=") for t in tags), dict(f.split("=") for f in fields.split(","))


class MetricsTests(Base):
    def env(self, fake=None):
        return {**super().env(), "INGEST_GRAFANA_PROM_URL": f"https://{HOST}", "INGEST_GRAFANA_PROM_USER": PROM_USER}

    def setUp(self):
        super().setUp()
        self.log.write_text("a line\n")

    def metrics_requests(self, f):
        return [r for r in f.requests if r["path"] == sg.METRICS_PATH]

    def test_payload_format(self):
        f = self.fake()
        out = self.run_ship(f)
        (r,) = self.metrics_requests(f)
        self.assertEqual(r["headers"]["Content-Type"], "text/plain")
        self.assertEqual(r["headers"]["Authorization"],
                         "Basic " + base64.b64encode(f"{PROM_USER}:{CANARY_TOKEN}".encode()).decode())
        meas, tags, fields = parse_influx(r["body"])
        self.assertEqual(meas, "cowlic_ingest")
        self.assertEqual(set(tags), {"job", "host"})  # nothing per track, per playlist or per class
        self.assertEqual(tags["job"], "cowlic-ingest")
        self.assertEqual(set(fields), FIELD_NAMES)
        for v in fields.values():
            self.assertRegex(v, r"^\d+i$")  # integers only: Influx strings are dropped by the server
        self.assertEqual(fields["added"], "2i")
        self.assertEqual(fields["skipped"], "1i")
        self.assertEqual(fields["success"], "1i")
        self.assertEqual(fields["exit_code"], "0i")
        self.assertEqual(fields["duration_seconds"], "159i")
        self.assertEqual(fields["last_run_timestamp_seconds"], f"{calendar.timegm((2026, 10, 10, 19, 12, 40))}i")
        self.assertTrue(all(fields[k] == "0i" for k in fields if k.startswith("failure_")))
        self.assertEqual(out.strip(), "grafana: loki push ok (HTTP 204); metrics push ok (HTTP 204)")

    def test_host_label_is_allowlisted_in_influx_body_and_loki_label(self):
        hostile = ["a b,c=d", "h\nx=1i", "h\\", "", "a" * 64, "h=1", "h\r", "h\n", "h,job=x", "höst"]
        for name in hostile:
            f = self.fake()
            with mock.patch.object(sg.socket, "gethostname", return_value=name):
                self.run_ship(f)
            _, tags, _ = parse_influx(self.metrics_requests(f)[0]["body"])
            self.assertEqual(tags["host"], "unknown", repr(name))
            loki = [r for r in f.requests if r["path"] == sg.PUSH_PATH][0]
            hosts = {s["stream"]["host"] for s in json.loads(loki["body"])["streams"]}
            self.assertEqual(hosts, {"unknown"}, repr(name))
        for name in ("my-host.local", "a" * 63):
            f = self.fake()
            with mock.patch.object(sg.socket, "gethostname", return_value=name):
                self.run_ship(f)
            _, tags, _ = parse_influx(self.metrics_requests(f)[0]["body"])
            self.assertEqual(tags["host"], name)
            loki = [r for r in f.requests if r["path"] == sg.PUSH_PATH][0]
            self.assertEqual({s["stream"]["host"] for s in json.loads(loki["body"])["streams"]}, {name})

    def test_failed_run_values(self):
        self.lr.write_text(json.dumps({**LAST_RUN, "exit_code": 3, "failure_class": "youtube-sign-in"}))
        f = self.fake()
        self.run_ship(f)
        _, _, fields = parse_influx(self.metrics_requests(f)[0]["body"])
        self.assertEqual((fields["success"], fields["exit_code"], fields["failure_youtube_sign_in"],
                          fields["failure_crash"]), ("0i", "3i", "1i", "0i"))

    def test_result_null_has_no_count_fields(self):
        self.lr.write_text(json.dumps({**LAST_RUN, "result": None}))
        f = self.fake()
        self.run_ship(f)
        _, _, fields = parse_influx(self.metrics_requests(f)[0]["body"])
        self.assertNotIn("added", fields)
        self.assertIn("success", fields)

    def test_payload_has_no_secret_or_free_text(self):
        # the playlist url, log text, the token, both users and the host name of the stack stay out of the body
        self.log.write_text(f"line with {CANARY_LINE_SECRET}\n")
        f = self.fake()
        self.run_ship(f)
        body = self.metrics_requests(f)[0]["body"].decode()
        for s in (CANARY_TOKEN, CANARY_USER, PROM_USER, CANARY_LINE_SECRET, "spotify", "line with", HOST, "http"):
            self.assertNotIn(s, body)
        self.assertNotIn(base64.b64encode(f"{PROM_USER}:{CANARY_TOKEN}".encode()).decode(), body)

    def test_final_guard_blocks_a_secret(self):
        f = self.fake()
        with mock.patch.object(sg, "metrics_line", return_value=f"x f={CANARY_TOKEN}\n"):
            out = self.run_ship(f)
        self.assertEqual(self.metrics_requests(f), [])
        self.assertIn("metrics skipped (secret found in payload, not sent)", out)

    def test_not_asked_for_without_keys(self):
        f = self.fake()
        out = self.run_ship(f, env={k: v for k, v in self.env().items() if "PROM" not in k})
        self.assertEqual(self.metrics_requests(f), [])
        self.assertEqual(out.strip(), "grafana: loki push ok (HTTP 204)")

    def test_one_key_alone_sends_nothing(self):
        for drop in ("INGEST_GRAFANA_PROM_URL", "INGEST_GRAFANA_PROM_USER"):
            f = self.fake()
            out = self.run_ship(f, env={k: v for k, v in self.env().items() if k != drop})
            self.assertEqual(self.metrics_requests(f), [])
            self.assertIn("metrics skipped (not configured)", out)

    def test_bad_credential_format(self):
        for v in ("a:b", "u\r\nX: 1", " padded"):
            with self.assertRaises(sg.ShipError):
                sg.read_metrics_config({**self.env(), "INGEST_GRAFANA_PROM_USER": v}, CANARY_TOKEN)

    def test_url_rules_and_path(self):
        self.assertEqual(sg.validate_url("https://prometheus-prod-01.grafana.net", sg.METRICS_PATH),
                         "https://prometheus-prod-01.grafana.net/api/v1/push/influx/write")
        self.assertEqual(sg.validate_url("https://x.grafana.net/api/v1/push/influx/write", sg.METRICS_PATH),
                         "https://x.grafana.net/api/v1/push/influx/write")
        for bad in ("https://evil.com", "http://x.grafana.net", "https://x.grafana.net/loki/api/v1/push",
                    "https://x.grafana.net/api/v1/push/influx/write?x=1", "https://u:p@x.grafana.net"):
            with self.assertRaises(sg.ShipError, msg=bad):
                sg.validate_url(bad, sg.METRICS_PATH)
        with self.assertRaises(sg.ShipError):  # the metrics path is not accepted for the log push
            sg.validate_url("https://x.grafana.net/api/v1/push/influx/write")

    def test_foreign_metrics_host_sends_nothing(self):
        f = self.fake()
        sent = []  # the real URL validation runs; post is replaced so that nothing can leave the machine
        env = {**self.env(), "INGEST_GRAFANA_PROM_URL": "https://evil.example.com"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(
                sg, "post", side_effect=lambda url, *a, **k: sent.append(url) or "ok (HTTP 204)"):
            sg.ship(str(self.log), 0, str(self.lr), env=env, redact=fake_redact)
        self.assertEqual(sent, [f"https://{HOST}/loki/api/v1/push"])  # the log push only
        self.assertIn("metrics skipped (host is not a *.grafana.net host)", out.getvalue())
        self.assertNotIn("evil", out.getvalue())
        self.assertEqual(f.requests, [])

    def test_failures_only_change_the_status_line(self):
        for code in (401, 429, 500):
            f = self.fake(code)
            out = self.run_ship(f)
            self.assertIn(f"metrics push failed (HTTP {code})", out)
            self.assertEqual(len(self.metrics_requests(f)), 1, "no retry")
            for s in (CANARY_TOKEN, CANARY_USER, PROM_USER, HOST):
                self.assertNotIn(s, out)
            self.assertEqual(out.splitlines()[0].count("grafana:"), 1)  # one line carries both results

    def test_metrics_failure_while_logs_ok(self):
        f = self.fake()
        real = sg.post

        def flaky(url, body, auth, opener=None, content_type="application/json"):
            if content_type == "text/plain":
                return "failed (URLError)"
            return real(url, body, auth, opener, content_type)
        with mock.patch.object(sg, "post", side_effect=flaky):
            out = self.run_ship(f)
        self.assertEqual(out.strip(), "grafana: loki push ok (HTTP 204); metrics push failed (URLError)")

    def test_unexpected_exception_in_metrics_is_contained(self):
        f = self.fake()
        with mock.patch.object(sg, "metrics_line", side_effect=RuntimeError(f"boom {CANARY_TOKEN}")):
            out = self.run_ship(f)
        self.assertEqual(out.strip(), "grafana: loki push ok (HTTP 204); metrics failed (RuntimeError)")
        self.assertNotIn(CANARY_TOKEN, out)

    def test_no_last_run_means_no_metrics(self):
        self.lr.write_text("garbage")
        f = self.fake()
        out = self.run_ship(f)
        self.assertEqual(self.metrics_requests(f), [])
        self.assertIn("metrics skipped (no last-run.json for this run)", out)

    def test_nan_in_last_run_means_no_metrics(self):
        self.lr.write_text(json.dumps(LAST_RUN).replace('"added": 2', '"added": NaN'))
        f = self.fake()
        self.run_ship(f)
        self.assertEqual(self.metrics_requests(f), [])

    def test_timeout_and_deadline(self):
        f = self.fake("hang")
        t = time.monotonic()
        out = self.run_ship(f, REQUEST_TIMEOUT_S=30, OVERALL_DEADLINE_S=0.5)
        self.assertLess(time.monotonic() - t, 6)
        self.assertIn("metrics push failed (deadline)", out)

    def test_redirect_not_followed(self):
        f = self.fake("redirect")
        out = self.run_ship(f)
        self.assertIn("metrics push failed (HTTP 302)", out)
        self.assertNotIn("/elsewhere", [r["path"] for r in f.requests])

    def test_main_exits_zero_whatever_happens(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env(), clear=True), \
                mock.patch("tg_check.load_env"), mock.patch.object(sg, "ship", side_effect=RuntimeError("x")):
            rc = sg.main(["--log", str(self.log), "--offset", "0", "--last-run", str(self.lr)])
        self.assertEqual(rc, 0)


class SliceTests(Base):
    def test_offset_must_be_at_line_start(self):
        self.log.write_text("abcdef\nrest\n")
        with self.assertRaises(sg.ShipError):
            sg.read_run_text(str(self.log), 3)
        self.assertEqual(sg.read_run_text(str(self.log), 7), ("rest\n", 0))
        self.assertEqual(sg.read_run_text(str(self.log), 0), ("abcdef\nrest\n", 0))

    def test_rotation_log_shorter_than_offset_ships_summary_only(self):
        self.log.write_text("short\n")
        f = self.fake()
        out = self.run_ship(f, offset=500)
        lines = [v[1] for s in json.loads(f.requests[0]["body"])["streams"] for v in s["values"]]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("run_summary"))
        self.assertIn("rotated", out)

    def test_missing_log_ships_summary_only(self):
        f = self.fake()
        self.run_ship(f)
        lines = [v[1] for s in json.loads(f.requests[0]["body"])["streams"] for v in s["values"]]
        self.assertEqual(len(lines), 1)

    def test_symlink_log_not_followed(self):
        target = Path(self.tmp.name, "secret.txt")
        target.write_text("PRIVATE\n")
        self.log.symlink_to(target)
        f = self.fake()
        self.run_ship(f)
        self.assertNotIn("PRIVATE", f.requests[0]["body"].decode())

    def test_oversized_slice_keeps_tail_and_drops_cut_line(self):
        with mock.patch.object(sg, "MAX_READ_BYTES", 100):
            self.log.write_text("".join(f"line-{i:04d}\n" for i in range(100)))
            text, omitted = sg.read_run_text(str(self.log), 0)
        self.assertGreater(omitted, 0)
        self.assertTrue(text.startswith("line-"))
        self.assertTrue(text.endswith("line-0099\n"))


class LastRunTests(Base):
    def parse(self, raw: str):
        self.lr.write_text(raw)
        return sg.parse_last_run(str(self.lr))

    def test_rejects_nan_infinity(self):
        for bad in ('{"exit_code": NaN}', '{"exit_code": Infinity}', '{"exit_code": -Infinity}'):
            with self.assertRaises(sg.ShipError):
                self.parse(bad)

    def test_strict_json_rejects_non_finite(self):
        for bad in (b'{"a": NaN}', b'[Infinity]', b'-Infinity'):
            with self.assertRaises(ValueError):
                sg.strict_json(bad)
        self.assertEqual(sg.strict_json(b'{"a": 1}'), {"a": 1})

    def test_date_that_matches_the_pattern_but_is_not_real_is_a_shipping_error(self):
        for bad in ("2026-13-10T19:10:01Z", "2026-02-30T19:10:01Z", "2026-10-10T25:10:01Z", "2026-10-10T19:60:01Z"):
            with self.assertRaises(sg.ShipError) as cm:
                sg._iso(bad)
            self.assertEqual(str(cm.exception), "last-run.json has an unexpected value")  # fixed text, no value
        for field in ("started_at", "finished_at"):
            with self.assertRaises(sg.ShipError):
                self.parse(json.dumps({**LAST_RUN, field: "2026-13-10T19:10:01Z"}))

    def test_impossible_date_still_ships_the_log_and_the_metrics(self):
        self.log.write_text("a line\n")
        self.lr.write_text(json.dumps({**LAST_RUN, "finished_at": "2026-13-10T19:12:40Z"}))
        f = self.fake()
        env = {**self.env(), "INGEST_GRAFANA_PROM_URL": f"https://{HOST}", "INGEST_GRAFANA_PROM_USER": PROM_USER}
        out = self.run_ship(f, env=env)
        self.assertIn("no run_summary (last-run.json has an unexpected value)", out)
        self.assertIn("loki push ok", out)
        self.assertNotIn("2026-13", out)
        self.assertEqual([r["path"] for r in f.requests].count(sg.PUSH_PATH), 1)
        self.assertIn("metrics skipped (no last-run.json for this run)", out)

    def test_last_run_of_another_run_is_ignored(self):
        self.assertEqual(self.parse(json.dumps(LAST_RUN))["exit_code"], 0)
        sg.parse_last_run(str(self.lr), "2026-10-10T19:10:01Z")
        with self.assertRaises(sg.ShipError):
            sg.parse_last_run(str(self.lr), "2026-10-09T19:10:01Z")

    def test_rejects_nan_inside_counts(self):
        bad = json.dumps(LAST_RUN).replace('"added": 2', '"added": NaN')
        with self.assertRaises(sg.ShipError):
            self.parse(bad)

    def test_rejects_garbage_and_bad_values(self):
        cases = ["not json", "[]", "{}", json.dumps({**LAST_RUN, "failure_class": "pwned class=1"}),
                 json.dumps({**LAST_RUN, "exit_code": True}), json.dumps({**LAST_RUN, "exit_code": 1.5}),
                 json.dumps({**LAST_RUN, "exit_code": -1}), json.dumps({**LAST_RUN, "started_at": "yesterday"}),
                 json.dumps({**LAST_RUN, "result": {"counts": {"added": "2 evil=1"}}})]
        for c in cases:
            with self.assertRaises(sg.ShipError, msg=c):
                self.parse(c)

    def test_parse_failure_means_no_summary_but_logs_ship(self):
        self.lr.write_text('{"exit_code": NaN}')
        self.log.write_text("a line\n")
        f = self.fake()
        out = self.run_ship(f)
        lines = [v[1] for s in json.loads(f.requests[0]["body"])["streams"] for v in s["values"]]
        self.assertEqual(lines, ["a line"])
        self.assertIn("no run_summary", out)

    def test_result_null_has_no_counts(self):
        run = self.parse(json.dumps({**LAST_RUN, "result": None}))
        self.assertNotIn("added=", sg.summary_line(run))

    def test_only_allowlisted_values_in_summary(self):
        d = {**LAST_RUN, "log_file": "SECRETNAME", "extra": "TOKEN123"}
        d["result"] = {"counts": {**LAST_RUN["result"]["counts"], "secret_key": 5}}
        line = sg.summary_line(self.parse(json.dumps(d)))
        self.assertNotIn("SECRETNAME", line)
        self.assertNotIn("TOKEN123", line)
        self.assertNotIn("secret_key", line)


class FailureTests(Base):
    def setUp(self):
        super().setUp()
        self.log.write_text("a line\n")

    def test_http_errors_are_reported_by_code_only(self):
        for code in (401, 429, 500):
            f = self.fake(code)
            out = self.run_ship(f)
            self.assertEqual(len(f.requests), 1, "no retry")
            self.assertEqual(out.strip(), f"grafana: loki push failed (HTTP {code})")

    def test_rejected_body_gets_one_summary_only_followup(self):
        for code in (400, 413):
            f = self.fake(code)
            out = self.run_ship(f)
            self.assertEqual(len(f.requests), 2)
            second = [v[1] for s in json.loads(f.requests[1]["body"])["streams"] for v in s["values"]]
            self.assertEqual(len(second), 1)
            self.assertTrue(second[0].startswith("run_summary"))
            self.assertIn(f"loki push failed (HTTP {code})", out)

    def test_no_followup_without_summary(self):
        self.lr.write_text("garbage")
        f = self.fake(400)
        self.run_ship(f)
        self.assertEqual(len(f.requests), 1)

    def test_body_size_capped_and_summary_kept(self):
        self.log.write_text("".join(f"{i:05d} " + "y" * 3000 + "\n" for i in range(1900)))
        f = self.fake()
        self.run_ship(f)
        body = f.requests[0]["body"]
        self.assertLess(len(body), sg.MAX_BODY_BYTES + 100_000)
        lines = [v[1] for s in json.loads(body)["streams"] for v in s["values"]]
        self.assertTrue(any("size cap" in ln for ln in lines))
        self.assertTrue(any(ln.startswith("run_summary") for ln in lines))
        self.assertTrue(lines[-2].startswith("01899 "))  # the tail of the run survives

    def test_unreachable_host(self):
        f = self.fake()
        f.close()  # port now refuses connections
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", return_value=f.url):
            sg.ship(str(self.log), 0, str(self.lr), env=self.env(), redact=fake_redact)
        self.assertRegex(out.getvalue(), r"^grafana: loki push failed \(URLError\)$")
        self.assertNotIn("127.0.0.1", out.getvalue())

    def test_timeout_returns_quickly(self):
        f = self.fake("hang")
        t = time.monotonic()
        out = self.run_ship(f, REQUEST_TIMEOUT_S=0.5)
        self.assertLess(time.monotonic() - t, 4)
        self.assertEqual(len(f.requests), 1)
        self.assertRegex(out, r"failed \((timeout|TimeoutError|URLError)\)")

    def test_overall_deadline(self):
        # a server that accepts but never answers, with the per-socket timeout longer than the deadline
        f = self.fake("hang")
        t = time.monotonic()
        out = self.run_ship(f, REQUEST_TIMEOUT_S=30, OVERALL_DEADLINE_S=0.5)
        self.assertLess(time.monotonic() - t, 4)
        self.assertIn("failed (deadline)", out)

    def test_redirect_not_followed(self):
        f = self.fake("redirect")
        out = self.run_ship(f)
        self.assertEqual(len(f.requests), 1)
        self.assertEqual(f.requests[0]["path"], "/loki/api/v1/push")
        self.assertIn("failed (HTTP 302)", out)

    def test_error_message_never_contains_secrets(self):
        f = self.fake(500)
        out = self.run_ship(f)
        for s in (CANARY_TOKEN, CANARY_USER, HOST):
            self.assertNotIn(s, out)


class ConfigTests(Base):
    def main(self, env, fake=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("tg_check.load_env"):
            rc = sg.main(["--log", str(self.log), "--offset", "0", "--last-run", str(self.lr)])
        return rc, out.getvalue()

    def test_missing_keys_no_request(self):
        self.log.write_text("x\n")
        f = self.fake()
        for drop in ("INGEST_GRAFANA_LOKI_URL", "INGEST_GRAFANA_LOKI_USER", "INGEST_GRAFANA_TOKEN"):
            env = {k: v for k, v in self.env().items() if k != drop}
            rc, out = self.main(env)
            self.assertEqual((rc, out.strip()), (0, "grafana: not configured"))
        rc, out = self.main({})
        self.assertEqual((rc, out.strip()), (0, "grafana: not configured"))
        self.assertEqual(f.requests, [])

    def test_metrics_keys_without_log_keys_say_why_nothing_was_sent(self):
        self.log.write_text("x\n")
        f = self.fake()
        env = {"INGEST_GRAFANA_TOKEN": CANARY_TOKEN, "INGEST_GRAFANA_PROM_URL": f"https://{HOST}",
               "INGEST_GRAFANA_PROM_USER": PROM_USER}
        for keys in (env, {k: v for k, v in env.items() if k != "INGEST_GRAFANA_PROM_USER"},
                     {k: v for k, v in env.items() if k != "INGEST_GRAFANA_PROM_URL"}):
            rc, out = self.main(keys)
            self.assertEqual(rc, 0)
            self.assertIn("not configured", out)
            self.assertIn("metrics skipped", out)
            self.assertNotIn(CANARY_TOKEN, out)
            self.assertNotIn(PROM_USER, out)
        rc, out = self.main({"INGEST_GRAFANA_TOKEN": CANARY_TOKEN})  # no metrics keys: the old plain line
        self.assertEqual(out.strip(), "grafana: not configured")
        self.assertEqual(f.requests, [])

    def test_validate_url(self):
        ok = ["https://logs-prod-012.grafana.net", "https://logs-prod-012.grafana.net/",
              "https://logs-prod-012.grafana.net/loki/api/v1/push", "HTTPS://A.GRAFANA.NET:443"]
        for u in ok:
            self.assertEqual(sg.validate_url(u).endswith(".grafana.net/loki/api/v1/push"), True, u)
        bad = ["http://logs.grafana.net", "https://evil.com", "https://grafana.net", "https://x.grafana.net.evil.com",
               "https://evilgrafana.net", "https://x.grafana.net@evil.com", "https://user:pw@x.grafana.net",
               "https://evil.com/x.grafana.net", "https://x.grafana.net:8443", "https://x.grafana.net/other",
               "https://x.grafana.net/loki/api/v1/push?x=1", "https://x.grafana.net/#f", "ftp://x.grafana.net",
               "https://x.grafana.net.", "https://127.0.0.1", "https://[::1]", "", "//x.grafana.net",
               "https://x.grafana.net\\@evil.com", "https://x_y.grafana.net"]
        for u in bad:
            with self.assertRaises(sg.ShipError, msg=u):
                sg.validate_url(u)

    def test_main_rejects_foreign_and_plain_http_and_sends_nothing(self):
        self.log.write_text("x\n")
        f = self.fake()
        for url in ("https://evil.example.com", f.url, "http://x.grafana.net"):
            rc, out = self.main({**self.env(), "INGEST_GRAFANA_LOKI_URL": url})
            self.assertEqual(rc, 0)
            self.assertTrue(out.startswith("grafana: "))
            self.assertNotIn(url, out)
            self.assertNotIn(CANARY_TOKEN, out)
        self.assertEqual(f.requests, [])

    def test_bad_credential_format(self):
        for k, v in (("INGEST_GRAFANA_TOKEN", "tok\r\nX-Evil: 1"), ("INGEST_GRAFANA_LOKI_USER", "a:b"),
                     ("INGEST_GRAFANA_TOKEN", " padded")):
            with self.assertRaises(sg.ShipError):
                sg.read_config({**self.env(), k: v})

    def test_main_always_exits_zero(self):
        self.log.write_text("x\n")
        with mock.patch.object(sg, "ship", side_effect=RuntimeError(f"boom {CANARY_TOKEN}")):
            rc, out = self.main(self.env())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "grafana: failed (RuntimeError)")
        with mock.patch.object(sg, "ship", side_effect=KeyboardInterrupt()):
            self.assertEqual(self.main(self.env())[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main(["--bogus"]), 0)

    def test_env_syntax_error_does_not_fail(self):
        import tg_check
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("tg_check.load_env", side_effect=tg_check.EnvSyntaxError("line 3 key X")):
            rc = sg.main(["--log", str(self.log), "--offset", "0", "--last-run", str(self.lr)])
        self.assertEqual(rc, 0)
        self.assertIn("env not loaded (EnvSyntaxError)", out.getvalue())
        self.assertNotIn("line 3", out.getvalue())

    def test_import_failure_of_redaction_sends_nothing(self):
        self.log.write_text("x\n")
        f = self.fake()
        import builtins
        real = builtins.__import__

        def deny(name, *a, **k):
            if name == "ingest":
                raise ImportError("no")
            return real(name, *a, **k)
        with mock.patch.object(sg, "validate_url", return_value=f.url), mock.patch("builtins.__import__", deny):
            rc, out = self.main(self.env())
        self.assertEqual((rc, out.strip()), (0, "grafana: failed (ImportError)"))
        self.assertEqual(f.requests, [])


class LogsOnlyTests(Base):
    """--job, --logs-only, follow mode (offset file, bounded batch) and the filter mode."""
    USER = "sentinelLfmUser"

    def setUp(self):
        super().setUp()
        self.log = Path(self.tmp.name, "lastfm-report.log")
        self.off = Path(self.tmp.name, ".offset")

    def logs_only(self, fake, offset=0, final=False, env=None, job="cowlic-report", offset_file=True, extra=()):
        out = io.StringIO()
        point = lambda raw, path=sg.PUSH_PATH: fake.base + path  # noqa: E731
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", side_effect=point):
            sg.ship_logs_only(str(self.log), offset, str(self.off) if offset_file else None, job, final,
                              env=env or self.env(), redact=fake_redact, extra_secrets=extra)
        return out.getvalue()

    def lines_of(self, req):
        return [v[1] for s in json.loads(req["body"])["streams"] for v in s["values"]]

    def test_job_label_and_default(self):
        self.log.write_text("one\n")
        f = self.fake()
        self.logs_only(f)
        jobs = {s["stream"]["job"] for s in json.loads(f.requests[0]["body"])["streams"]}
        self.assertEqual(jobs, {"cowlic-report"})
        self.assertEqual(sg.build_payload(["x"], None, "info", 1, 2, "h")["streams"][0]["stream"]["job"],
                         "cowlic-ingest")  # the nightly default

    def test_job_allowlist(self):
        for bad in ("", "Cowlic", "a b", "a\"b", "1abc", "a" * 33, 'x"}', "a;b", "../x"):
            with self.assertRaises(Exception, msg=bad):
                sg._job(bad)
        self.assertEqual(sg._job("cowlic-report"), "cowlic-report")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "ship_logs_only") as m:
            self.assertEqual(sg.main(["--logs-only", "--job", "Bad Job", "--log", "x", "--offset", "0"]), 0)
        m.assert_not_called()
        self.assertIn("bad arguments", out.getvalue())

    def test_logs_only_sends_no_metrics_and_no_summary(self):
        self.log.write_text("one\ntwo\n")
        f = self.fake()
        env = dict(self.env(), INGEST_GRAFANA_PROM_URL=f"https://{HOST}", INGEST_GRAFANA_PROM_USER="42")
        with mock.patch.object(sg, "ship_metrics") as m:
            self.logs_only(f, env=env)
        m.assert_not_called()
        self.assertEqual([r["path"] for r in f.requests], ["/loki/api/v1/push"])
        self.assertEqual(self.lines_of(f.requests[0]), ["one", "two"])
        self.assertFalse(any("run_summary" in ln for ln in self.lines_of(f.requests[0])))

    def test_main_logs_only_needs_no_last_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "ship_logs_only") as m, \
                mock.patch.object(sg, "ship") as nightly:
            sg.main(["--logs-only", "--job", "cowlic-report", "--log", "x", "--offset", "0", "--offset-file", "o"])
        m.assert_called_once()
        nightly.assert_not_called()

    def test_incremental_offset_moves_and_ships_each_line_once(self):
        f = self.fake()
        self.log.write_text("a1\na2\n")
        self.logs_only(f)
        self.assertEqual(self.off.read_text().strip(), str(len("a1\na2\n")))
        self.assertEqual(oct(self.off.stat().st_mode & 0o777), "0o600")
        with open(self.log, "a") as fh:
            fh.write("b1\nunfinished")
        self.logs_only(f)
        self.assertEqual([self.lines_of(r) for r in f.requests], [["a1", "a2"], ["b1"]])
        self.logs_only(f)  # nothing new: no push
        self.assertEqual(len(f.requests), 2)
        self.logs_only(f, final=True)  # the final call also takes the unfinished line
        self.assertEqual(self.lines_of(f.requests[2]), ["unfinished"])

    def test_initial_offset_skips_older_lines(self):
        self.log.write_text("old\nnew\n")
        f = self.fake()
        self.logs_only(f, offset=len("old\n"))
        self.assertEqual(self.lines_of(f.requests[0]), ["new"])

    def test_failed_push_keeps_offset_for_retry_but_content_rejection_moves_on(self):
        self.log.write_text("a\n")
        f = self.fake(500)
        self.logs_only(f)
        self.assertFalse(self.off.exists())
        g = self.fake(429)
        self.logs_only(g)
        self.assertFalse(self.off.exists())
        h = self.fake(400)
        self.logs_only(h)
        self.assertEqual(self.off.read_text().strip(), "2")

    def test_batch_is_bounded_and_cut_at_a_line(self):
        with mock.patch.object(sg, "MAX_BATCH_BYTES", 25):
            self.log.write_text("".join(f"line-{i:03d}\n" for i in range(10)))  # 9 bytes per line
            f = self.fake()
            self.logs_only(f)
            self.assertEqual(self.lines_of(f.requests[0]), ["line-000", "line-001"])
            self.assertEqual(self.off.read_text().strip(), "18")
            self.logs_only(f)
            self.assertEqual(self.lines_of(f.requests[1]), ["line-002", "line-003"])

    def test_rotated_log_or_bad_offset_sends_nothing(self):
        self.log.write_text("short\n")
        self.off.write_text("500\n")
        f = self.fake()
        out = self.logs_only(f)
        self.assertEqual(f.requests, [])
        self.assertIn("rotated", out)
        self.off.write_text("3\n")  # not at a line start
        self.logs_only(f)
        self.assertEqual(f.requests, [])

    def test_corrupt_offset_file_is_reset_with_a_status_line(self):
        self.log.write_text("a\nb\n")
        self.off.write_text("-1")
        f = self.fake()
        out = self.logs_only(f)
        self.assertIn("reset", out)
        self.assertEqual(self.lines_of(f.requests[0]), ["a", "b"])
        self.assertEqual(self.off.read_text().strip(), "4")

    def test_overlong_line_is_cut_once_and_never_split(self):
        with mock.patch.object(sg, "MAX_BATCH_BYTES", 20):
            self.log.write_text("short\n" + "x" * 50 + "\nafter\n")
            f = self.fake()
            self.logs_only(f)  # "short\n" (6 bytes): the long line does not fit the batch, so only "short"
            self.assertEqual(self.lines_of(f.requests[0]), ["short"])
            self.logs_only(f)  # the long line alone: first 20 bytes shipped, the rest skipped to its newline
            self.assertEqual(self.lines_of(f.requests[1]), ["x" * 20])
            self.assertEqual(self.off.read_text().strip(), str(len("short\n" + "x" * 50 + "\n")))
            self.logs_only(f)  # the following line is not wedged
            self.assertEqual(self.lines_of(f.requests[2]), ["after"])

    def test_overlong_unfinished_line_waits_then_final_takes_it(self):
        with mock.patch.object(sg, "MAX_BATCH_BYTES", 20):
            self.log.write_text("y" * 50)
            f = self.fake()
            self.logs_only(f)
            self.assertEqual(f.requests, [])
            self.logs_only(f, final=True)
            self.assertEqual(self.lines_of(f.requests[0]), ["y" * 20])
            self.assertEqual(self.off.read_text().strip(), "50")

    def test_log_without_trailing_newline_does_not_wedge_a_fresh_offset(self):
        # an offset saved at the end of a finished log is a line start; a final push of an unfinished line
        # moves the offset to EOF (the wrapper never appends to such a log: a fresh log per run)
        self.log.write_text("a\nunfinished")
        f = self.fake()
        self.logs_only(f)
        self.assertEqual(self.lines_of(f.requests[0]), ["a"])
        self.logs_only(f, final=True)
        self.assertEqual(self.lines_of(f.requests[1]), ["unfinished"])

    def test_logs_only_refuses_the_nightly_job_label(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "ship_logs_only") as m:
            sg.main(["--logs-only", "--job", "cowlic-ingest", "--log", "x", "--offset", "0"])
        m.assert_not_called()
        self.assertIn("--job", out.getvalue())

    def test_main_redacts_the_env_secret_in_the_shipped_body(self):
        self.log.write_text(f"hello {self.USER} and {self.USER.upper()}\n")
        f = self.fake()
        env = dict(self.env(), **{sg.EXTRA_SECRET_ENV: self.USER})
        point = lambda raw, path=sg.PUSH_PATH: f.base + path  # noqa: E731
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"tg_check": mock.Mock()}), \
                contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", side_effect=point):
            sg.main(["--logs-only", "--job", "cowlic-report", "--log", str(self.log), "--offset", "0",
                     "--offset-file", str(self.off)])
        self.assertEqual(len(f.requests), 1)
        body = f.requests[0]["body"].decode()
        self.assertNotIn(self.USER.lower(), body.lower())
        self.assertIn("***", body)

    def test_guard_refusal_prints_a_status_line(self):
        self.log.write_text("some co text\n")  # a user name "co" makes redaction and the guard bite everywhere
        f = self.fake()
        env = dict(self.env(), **{sg.EXTRA_SECRET_ENV: "ZZ"})
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"tg_check": mock.Mock()}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(sg, "ship_logs_only",
                                                                    side_effect=sg.ShipError("secret found in payload, not sent")):
                sg.main(["--logs-only", "--job", "cowlic-report", "--log", str(self.log), "--offset", "0"])
        self.assertIn("grafana: secret found in payload", out.getvalue())

    def test_redaction_of_extra_secret_token_and_final_guard(self):
        self.log.write_text(f"user {self.USER} token {CANARY_TOKEN} line\n")
        f = self.fake()
        self.logs_only(f, extra=[self.USER])
        body = f.requests[0]["body"].decode()
        self.assertNotIn(self.USER, body)
        self.assertNotIn(CANARY_TOKEN, body)
        self.assertNotIn(self.USER.lower(), body.lower())
        # the final guard: a redactor that lets the token through must make the push refuse
        self.log.write_text(f"leak {CANARY_TOKEN}\n")
        self.off.unlink()
        g = self.fake()
        with self.assertRaises(sg.ShipError):
            out = io.StringIO()
            point = lambda raw, path=sg.PUSH_PATH: g.base + path  # noqa: E731
            with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", side_effect=point):
                sg.ship_logs_only(str(self.log), 0, str(self.off), "cowlic-report", False, env=self.env(),
                                  redact=lambda t, s: t)
        self.assertEqual(g.requests, [])

    def test_url_validation_is_reused(self):
        self.log.write_text("a\n")
        env = dict(self.env(), INGEST_GRAFANA_LOKI_URL="https://evil.example.com")
        with self.assertRaises(sg.ShipError):
            sg.ship_logs_only(str(self.log), 0, None, "cowlic-report", False, env=env, redact=fake_redact)

    def test_filter_redacts_user_and_key_per_line(self):
        src = io.BytesIO(f"hello {self.USER}\nkey {CANARY_TOKEN}\x1b[31m red\nplain\n".encode())
        dst = io.BytesIO()
        sg.filter_stream(src, dst, [self.USER, CANARY_TOKEN], fake_redact)
        out = dst.getvalue().decode()
        self.assertNotIn(self.USER, out)
        self.assertNotIn(CANARY_TOKEN, out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(out.count("\n"), 3)
        self.assertIn("plain\n", out)

    def test_filter_fails_closed_per_line(self):
        def boom(text, secrets):
            raise RuntimeError("x")
        dst = io.BytesIO()
        sg.filter_stream(io.BytesIO(b"secret stuff\n"), dst, ["s"], boom)
        self.assertEqual(dst.getvalue(), b"[line withheld: redaction failed]\n")


if __name__ == "__main__":
    unittest.main()

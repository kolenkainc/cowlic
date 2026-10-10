"""Tests for ship_grafana: a fake Loki endpoint on 127.0.0.1 (in-process), temp files, no network, no Grafana.
validate_url is patched to point at the fake server only where a request must actually be sent; its own rules
are tested directly, and main() with a foreign host must send nothing."""
import base64
import contextlib
import io
import json
import os
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
        self.url = f"http://127.0.0.1:{self.port}/loki/api/v1/push"
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
        with contextlib.redirect_stdout(out), mock.patch.object(sg, "validate_url", return_value=fake.url):
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
        for code in (400, 401, 429, 500):
            f = self.fake(code)
            out = self.run_ship(f)
            self.assertEqual(len(f.requests), 1, "no retry")
            self.assertEqual(out.strip(), f"grafana: loki push failed (HTTP {code})")

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


if __name__ == "__main__":
    unittest.main()

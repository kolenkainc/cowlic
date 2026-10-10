"""Tests for spotify_user: fake HTTP, no Spotify, no browser. The listener tests talk to a local socket on
127.0.0.1 only (a free port picked per test), never to the network."""
import base64
import contextlib
import hashlib
import io
import logging
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import ingest
import spotify_user as su
import tg_check

CANARY_REFRESH = "canaryRefresh-0123456789abcdefXYZ"
CANARY_NEW = "canaryRotated-9876543210zyxwvuTSR"
CANARY_ACCESS = "canaryAccess-abcdefghij0123456789"
KEY = su.REFRESH_KEY


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


class FakePost:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body if body is not None else {}, []

    def __call__(self, url, form, timeout):
        self.calls.append((url, dict(form), timeout))
        return self.status, self.body


class Pkce(unittest.TestCase):
    def test_verifier_length_and_alphabet(self):
        for _ in range(50):
            v = su.make_verifier()
            self.assertTrue(43 <= len(v) <= 128)
            self.assertRegex(v, r"[A-Za-z0-9_.~-]+")
        self.assertNotEqual(su.make_verifier(), su.make_verifier())

    def test_challenge_is_s256_base64url_without_padding(self):
        v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636 appendix B
        self.assertEqual(su.make_challenge(v), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        v = su.make_verifier()
        want = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
        self.assertEqual(su.make_challenge(v), want)
        self.assertNotIn("=", su.make_challenge(v))

    def test_authorize_url(self):
        u = urllib.parse.urlsplit(su.authorize_url("cid", "chal", "st"))
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "accounts.spotify.com", "/authorize"))
        q = urllib.parse.parse_qs(u.query)
        self.assertEqual({k: v[0] for k, v in q.items()}, {
            "response_type": "code", "client_id": "cid", "redirect_uri": "http://127.0.0.1:8888/callback",
            "scope": "user-follow-read", "code_challenge_method": "S256", "code_challenge": "chal", "state": "st"})
        self.assertNotIn("client_secret", u.query)

    def test_constants(self):
        self.assertEqual(su.SCOPE, "user-follow-read")
        self.assertEqual(su.REDIRECT_URI, "http://127.0.0.1:8888/callback")
        self.assertEqual(su.HOST, "127.0.0.1")


class ParseCallback(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(su.parse_callback("code=abc&state=S1", "S1"), "abc")

    def test_state_mismatch_missing_or_extra(self):
        bad = ["code=abc&state=WRONG", "code=abc", "state=S1", "", "code=abc&state=S1&extra=1",
               "code=abc&state=S1&state=S1", "code=abc&code=def&state=S1", "code=&state=S1",
               "code=abc&error=access_denied&state=S1", "error=access_denied", "state=S1&error=access_denied&x=1"]
        for q in bad:
            with self.subTest(q=q), self.assertRaises(su.UserAuthError):
                su.parse_callback(q, "S1")

    def test_access_denied(self):
        with self.assertRaisesRegex(su.UserAuthError, "access_denied"):
            su.parse_callback("error=access_denied&state=S1", "S1")

    def test_error_with_wrong_state_is_a_state_failure(self):
        with self.assertRaisesRegex(su.UserAuthError, "state"):
            su.parse_callback("error=access_denied&state=X", "S1")

    def test_error_text_is_not_echoed(self):
        with self.assertRaises(su.UserAuthError) as cm:
            su.parse_callback("error=<script>evil</script>&state=S1", "S1")
        self.assertNotIn("evil", str(cm.exception))

    def test_state_compared_in_constant_time(self):
        with mock.patch.object(su.hmac, "compare_digest", wraps=su.hmac.compare_digest) as cd:
            su.parse_callback("code=abc&state=S1", "S1")
        cd.assert_called_once()


class Listener(unittest.TestCase):
    def run_listener(self, port, state="S1", timeout=5.0):
        res = {}

        def target():
            try:
                res["code"] = su.wait_for_callback(state, timeout, port)
            except su.UserAuthError as e:
                res["err"] = e
        t = threading.Thread(target=target)
        t.start()
        for _ in range(100):  # wait until it listens
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        return t, res

    def test_success_ignores_other_paths(self):
        port = free_port()
        t, res = self.run_listener(port)
        self.assertEqual(get(port, "/favicon.ico"), 404)
        self.assertEqual(get(port, "/callback?code=abc&state=S1"), 200)
        t.join(5)
        self.assertEqual(res, {"code": "abc"})

    def test_state_mismatch_is_rejected(self):
        port = free_port()
        t, res = self.run_listener(port)
        self.assertEqual(get(port, "/callback?code=abc&state=EVIL"), 400)
        t.join(5)
        self.assertNotIn("code", res)
        self.assertIn("state", str(res["err"]))

    def test_access_denied(self):
        port = free_port()
        t, res = self.run_listener(port)
        get(port, "/callback?error=access_denied&state=S1")
        t.join(5)
        self.assertIn("access_denied", str(res["err"]))

    def test_timeout(self):
        port = free_port()
        t0 = time.monotonic()
        with self.assertRaisesRegex(su.UserAuthError, "timed out"):
            su.wait_for_callback("S1", 0.5, port)
        self.assertLess(time.monotonic() - t0, 5)

    def test_idle_connection_does_not_block_a_valid_callback(self):
        port = free_port()
        with mock.patch.object(su, "CONN_TIMEOUT", 0.5):
            t, res = self.run_listener(port, timeout=10.0)
            idle = socket.create_connection(("127.0.0.1", port), timeout=5)  # connects, sends nothing
            self.addCleanup(idle.close)
            t0 = time.monotonic()
            self.assertEqual(get(port, "/callback?code=abc&state=S1"), 200)
            t.join(10)
        self.assertEqual(res, {"code": "abc"})
        self.assertLess(time.monotonic() - t0, 5)

    def test_overall_deadline_holds_with_only_idle_connections(self):
        port = free_port()
        socks = []
        res = {}

        def target():
            try:
                su.wait_for_callback("S1", 1.0, port)
            except su.UserAuthError as e:
                res["err"] = e
        t = threading.Thread(target=target)
        t0 = time.monotonic()
        t.start()
        for _ in range(100):
            try:
                socks.append(socket.create_connection(("127.0.0.1", port), timeout=5))
                break
            except OSError:
                time.sleep(0.05)
        for _ in range(3):
            try:
                socks.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            except OSError:
                break
        for s in socks:
            self.addCleanup(s.close)
        t.join(8)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 4)  # CONN_TIMEOUT is 5 s: only the deadline can end it this soon
        self.assertIn("timed out", str(res["err"]))

    def test_trickling_client_cannot_extend_the_deadline(self):
        """One byte every 0.5 s restarts a per-recv socket timeout forever; the total-time cap must end it."""
        port = free_port()
        res = {}
        stop = threading.Event()

        def listener():
            try:
                su.wait_for_callback("S1", 2.0, port)
            except su.UserAuthError as e:
                res["err"] = e

        def trickle():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                    s.sendall(b"G")
                    for _ in range(40):
                        if stop.wait(0.5):
                            break
                        s.sendall(b"E")
            except OSError:
                pass
        t = threading.Thread(target=listener)
        c = threading.Thread(target=trickle)
        t0 = time.monotonic()
        t.start()
        time.sleep(0.2)
        c.start()
        t.join(8)
        stop.set()
        c.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 3.5)
        self.assertIn("timed out", str(res["err"]))
        self.assertFalse([x for x in threading.enumerate() if isinstance(x, threading.Timer)])

    def test_too_many_query_parameters_is_a_clean_error(self):
        q = "&".join(f"p{i}=1" for i in range(20)) + "&state=S1&code=abc"
        with self.assertRaisesRegex(su.UserAuthError, "too many"):
            su.parse_callback(q, "S1")

    def test_binds_loopback_only(self):
        seen = {}
        real = su.HTTPServer

        def spy(addr, handler):
            seen["addr"] = addr
            return real(addr, handler)
        with mock.patch.object(su, "HTTPServer", spy), self.assertRaises(su.UserAuthError):
            su.wait_for_callback("S1", 0.2, free_port())
        self.assertEqual(seen["addr"][0], "127.0.0.1")

    def test_port_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            with self.assertRaisesRegex(su.UserAuthError, "cannot listen"):
                su.wait_for_callback("S1", 0.2, s.getsockname()[1])


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"

    def write(self, data: bytes, mode=0o640):
        self.env.write_bytes(data)
        os.chmod(self.env, mode)

    ORIGINAL = (b"# my comment \xc3\xa9\r\n\r\nTG_BOT_TOKEN=fake-abc\r\n  # SPOTIFY_USER_REFRESH_TOKEN=commented\r\n"
                b"SPOTIFY_CLIENT_ID='quoted id'\r\nLAST=1")

    def test_append_keeps_other_bytes(self):
        self.write(self.ORIGINAL)
        su.store_refresh_token("tok-1", self.env)
        self.assertEqual(self.env.read_bytes(), self.ORIGINAL + b"\n" + f"{KEY}=tok-1\n".encode())
        self.assertEqual(tg_check.parse_env(self.env.read_bytes())[KEY], "tok-1")

    def test_replace_existing_keeps_other_bytes(self):
        before = b"A=1\n# c\n" + f"{KEY}=old\n".encode() + b"B=2\n"
        self.write(before)
        su.store_refresh_token("tok-2", self.env)
        self.assertEqual(self.env.read_bytes(), b"A=1\n# c\n" + f"{KEY}=tok-2\n".encode() + b"B=2\n")

    def test_replace_keeps_crlf_and_empty_value(self):
        self.write(f"A=1\r\n{KEY}=\r\nB=2\r\n".encode())
        su.store_refresh_token("tok-3", self.env)
        self.assertEqual(self.env.read_bytes(), f"A=1\r\n{KEY}=tok-3\r\nB=2\r\n".encode())

    def test_mode_is_kept_and_no_temp_left(self):
        self.write(b"A=1\n", 0o600)
        su.store_refresh_token("tok-4", self.env)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.write(b"A=1\n", 0o640)
        su.store_refresh_token("tok-5", self.env)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o640)
        self.assertEqual(os.listdir(self.dir.name), [".env"])

    def test_missing_file_created_0600(self):
        su.store_refresh_token("tok-6", self.env)
        self.assertEqual(self.env.read_bytes(), f"{KEY}=tok-6\n".encode())
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_write_is_atomic(self):
        """The new content goes to a temp file in the same directory and replaces .env in one os.replace; if the
        replace fails the original is untouched and no temp file is left."""
        self.write(self.ORIGINAL)
        calls = []
        real = os.replace

        def spy(src, dst):
            calls.append((src, dst))
            self.assertEqual(Path(src).parent, Path(dst).parent)
            self.assertEqual(self.env.read_bytes(), self.ORIGINAL)  # not yet modified in place
            return real(src, dst)
        with mock.patch.object(su.os, "replace", spy):
            su.store_refresh_token("tok-7", self.env)
        self.assertEqual(len(calls), 1)

        self.write(self.ORIGINAL)
        with mock.patch.object(su.os, "replace", side_effect=OSError("boom")), self.assertRaises(OSError):
            su.store_refresh_token("tok-8", self.env)
        self.assertEqual(self.env.read_bytes(), self.ORIGINAL)
        self.assertEqual(os.listdir(self.dir.name), [".env"])

    def test_symlinked_env_keeps_the_link(self):
        real = Path(self.dir.name) / "real.env"
        real.write_bytes(b"A=1\n")
        self.env.symlink_to(real)
        su.store_refresh_token("tok-9", self.env)
        self.assertTrue(self.env.is_symlink())
        self.assertEqual(tg_check.parse_env(real.read_bytes())[KEY], "tok-9")

    def test_refuses_unsafe_token_and_bad_env(self):
        self.write(b"A=1\n")
        for bad in ["a b", "a#b", 'a"b', "a\nB=2", "", "toké", "x" * 3000]:
            with self.subTest(bad=bad), self.assertRaises(su.UserAuthError):
                su.store_refresh_token(bad, self.env)
        self.assertEqual(self.env.read_bytes(), b"A=1\n")
        self.write(b"export A=1\n")
        with self.assertRaises(tg_check.EnvSyntaxError):
            su.store_refresh_token("tok", self.env)
        self.assertEqual(self.env.read_bytes(), b"export A=1\n")

    def test_realistic_token_shapes_parse(self):
        self.write(b"A=1\n")
        tok = "AQD" + "aZ09_-" * 40 + "=="
        su.store_refresh_token(tok, self.env)
        self.assertEqual(tg_check.parse_env(self.env.read_bytes())[KEY], tok)


class Refresh(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        self.env.write_bytes(f"SPOTIFY_CLIENT_ID=cid\n{KEY}={CANARY_REFRESH}\n".encode())
        p = mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "cid", KEY: CANARY_REFRESH})
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(su.tg_check, "load_env")
        q.start()
        self.addCleanup(q.stop)

    def test_success_request_shape(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "token_type": "Bearer", "expires_in": 3600})
        self.assertEqual(su.get_access_token(post, self.env), CANARY_ACCESS)
        url, form, timeout = post.calls[0]
        self.assertEqual(url, "https://accounts.spotify.com/api/token")
        self.assertEqual(form, {"grant_type": "refresh_token", "refresh_token": CANARY_REFRESH, "client_id": "cid"})
        self.assertLessEqual(timeout, 30)
        self.assertIn(CANARY_REFRESH.encode(), self.env.read_bytes())  # unchanged

    def test_new_refresh_token_is_stored(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "refresh_token": CANARY_NEW})
        su.get_access_token(post, self.env)
        data = tg_check.parse_env(self.env.read_bytes())
        self.assertEqual(data[KEY], CANARY_NEW)
        self.assertEqual(data["SPOTIFY_CLIENT_ID"], "cid")
        self.assertEqual(os.environ[KEY], CANARY_NEW)

    def test_invalid_grant_says_log_in_again(self):
        post = FakePost(400, {"error": "invalid_grant", "error_description": f"bad {CANARY_REFRESH}"})
        with self.assertRaises(su.UserAuthError) as cm:
            su.get_access_token(post, self.env)
        self.assertIn("login", str(cm.exception))
        self.assertIn("invalid_grant", str(cm.exception))
        self.assertNotIn(CANARY_REFRESH, str(cm.exception))

    def test_other_errors_and_garbage(self):
        for status, body in [(500, {}), (400, {"error": "invalid_client"}), (200, {}), (200, {"access_token": ""}),
                             (429, {"error": "Mixed Case Text " + CANARY_REFRESH})]:
            with self.subTest(status=status, body=body), self.assertRaises(su.UserAuthError) as cm:
                su.get_access_token(FakePost(status, body), self.env)
            self.assertNotIn(CANARY_REFRESH, str(cm.exception))

    def test_missing_refresh_token(self):
        with mock.patch.dict(os.environ):
            del os.environ[KEY]
            with self.assertRaisesRegex(su.UserAuthError, "login"):
                su.get_access_token(FakePost(), self.env)

    def test_missing_client_id(self):
        with mock.patch.dict(os.environ):
            del os.environ["SPOTIFY_CLIENT_ID"]
            with self.assertRaisesRegex(su.UserAuthError, "SPOTIFY_CLIENT_ID"):
                su.get_access_token(FakePost(), self.env)

    def test_failed_store_error_has_no_token(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "refresh_token": "bad token with spaces " + CANARY_NEW})
        with self.assertRaises(su.UserAuthError) as cm:
            su.get_access_token(post, self.env)
        for c in (CANARY_NEW, CANARY_ACCESS, CANARY_REFRESH):
            self.assertNotIn(c, str(cm.exception))
        self.assertIn(CANARY_REFRESH.encode(), self.env.read_bytes())

    def test_network_error_has_no_details(self):
        with mock.patch.object(su.urllib.request, "urlopen", side_effect=urllib.error.URLError("dns " + CANARY_REFRESH)):
            with self.assertRaises(su.UserAuthError) as cm:
                su.default_post(su.TOKEN_URL, {}, 1)
        self.assertNotIn(CANARY_REFRESH, str(cm.exception))


class Login(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = Path(self.dir.name) / ".env"
        self.env.write_bytes(b"SPOTIFY_CLIENT_ID=cid\n# keep\n")
        p = mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "cid"})
        p.start()
        self.addCleanup(p.stop)

    def run_login(self, post, callback):
        """callback(url_query_dict) -> the callback query string; replaces the real listener."""
        lines = []

        def fake_wait(state, timeout, port):
            url = [ln for ln in lines if ln.startswith("https://")][0]
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
            return su.parse_callback(callback(q), state)
        out, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        err_obj = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(su, "wait_for_callback", fake_wait):
            try:
                su.login(post, self.env, 1.0, 8888, out=lambda s: (lines.append(s), print(s)))
            except su.UserAuthError as e:
                err_obj = e
        return out.getvalue() + err.getvalue() + logs.getvalue(), err_obj

    def test_success_flow(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "refresh_token": CANARY_REFRESH, "scope": su.SCOPE})
        seen = {}

        def cb(q):
            seen.update(q)
            return f"code=THECODE&state={q['state']}"
        printed, err = self.run_login(post, cb)
        self.assertIsNone(err)
        self.assertEqual(seen["scope"], "user-follow-read")
        self.assertEqual(seen["code_challenge_method"], "S256")
        _, form, _ = post.calls[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "THECODE")
        self.assertEqual(form["redirect_uri"], su.REDIRECT_URI)
        self.assertEqual(form["client_id"], "cid")
        self.assertNotIn("client_secret", form)
        self.assertEqual(su.make_challenge(form["code_verifier"]), seen["code_challenge"])
        self.assertEqual(tg_check.parse_env(self.env.read_bytes())[KEY], CANARY_REFRESH)
        self.assertTrue(self.env.read_bytes().startswith(b"SPOTIFY_CLIENT_ID=cid\n# keep\n"))
        for c in (CANARY_REFRESH, CANARY_ACCESS, form["code_verifier"]):
            self.assertNotIn(c, printed)
        self.assertIn(KEY, printed)

    def test_state_mismatch_stops_before_token_call(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "refresh_token": CANARY_REFRESH})
        _, err = self.run_login(post, lambda q: "code=THECODE&state=forged")
        self.assertIn("state", str(err))
        self.assertEqual(post.calls, [])
        self.assertNotIn(KEY.encode(), self.env.read_bytes())

    def test_access_denied(self):
        post = FakePost()
        _, err = self.run_login(post, lambda q: f"error=access_denied&state={q['state']}")
        self.assertIn("access_denied", str(err))
        self.assertEqual(post.calls, [])

    def test_no_refresh_token_in_response(self):
        _, err = self.run_login(FakePost(200, {"access_token": CANARY_ACCESS}),
                                lambda q: f"code=c&state={q['state']}")
        self.assertIn("no refresh token", str(err))
        self.assertNotIn(CANARY_ACCESS, str(err))

    def test_failed_store_does_not_leak(self):
        post = FakePost(200, {"access_token": CANARY_ACCESS, "refresh_token": CANARY_REFRESH})
        with mock.patch.object(su.os, "replace", side_effect=OSError(f"boom {CANARY_REFRESH}")):
            printed, err = self.run_login(post, lambda q: f"code=c&state={q['state']}")
        self.assertIsNotNone(err)
        self.assertNotIn(CANARY_REFRESH, str(err) + printed)
        self.assertNotIn(CANARY_ACCESS, str(err) + printed)

    def test_exchange_error_is_redacted(self):
        post = FakePost(400, {"error": "invalid_grant", "error_description": "x"})
        printed, err = self.run_login(post, lambda q: f"code=THECODE&state={q['state']}")
        self.assertNotIn("THECODE", str(err) + printed)
        self.assertIn("invalid_grant", str(err))


class Integration(unittest.TestCase):
    def test_required_env_does_not_include_the_refresh_key(self):
        self.assertNotIn(KEY, ingest.REQUIRED_ENV)

    def test_spotdl_does_not_get_the_refresh_token(self):
        self.assertIn(KEY, ingest.SPOTDL_HIDDEN_ENV)


if __name__ == "__main__":
    unittest.main()

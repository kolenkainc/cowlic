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


def aid(n: int) -> str:
    return f"id{n:05d}"


def page(ids, after=None, total=None):
    body = {"artists": {"items": [{"id": aid(i), "name": f"Artist {i}", "type": "artist"} for i in ids],
                        "total": total if total is not None else len(ids), "cursors": {"after": after},
                        "limit": 50, "href": "x"}}
    return 200, body, {"retry-after": ""}


class FakeGet:
    """Serves the queued (status, body, headers) tuples in order and records every call."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return self.responses.pop(0)


class Followed(unittest.TestCase):
    def query(self, url):
        u = urllib.parse.urlsplit(url)
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "api.spotify.com", "/v1/me/following"))
        return {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def test_paging_follows_the_cursor_and_stops_at_the_end(self):
        get = FakeGet(page(range(1, 51), after=aid(50), total=120), page(range(51, 101), after=aid(100), total=120),
                      page(range(101, 121), after=None, total=120))
        artists, total = su.fetch_followed(CANARY_ACCESS, get, sleep=lambda s: self.fail("no sleep"))
        self.assertEqual(total, 120)
        self.assertEqual([a["id"] for a in artists], [aid(i) for i in range(1, 121)])
        self.assertEqual(artists[0], {"id": aid(1), "name": "Artist 1"})
        qs = [self.query(c[0]) for c in get.calls]
        self.assertEqual(qs, [{"type": "artist", "limit": "50"},
                              {"type": "artist", "limit": "50", "after": aid(50)},
                              {"type": "artist", "limit": "50", "after": aid(100)}])
        for _, headers, timeout in get.calls:
            self.assertEqual(headers["Authorization"], f"Bearer {CANARY_ACCESS}")
            self.assertLessEqual(timeout, 30)

    def test_empty_list(self):
        get = FakeGet(page([], after=None, total=0))
        self.assertEqual(su.fetch_followed(CANARY_ACCESS, get), ([], 0))
        self.assertEqual(len(get.calls), 1)

    def test_followed_artists_returns_the_list(self):
        self.assertEqual(su.followed_artists(CANARY_ACCESS, FakeGet(page([1, 2], total=2))),
                         [{"id": aid(1), "name": "Artist 1"}, {"id": aid(2), "name": "Artist 2"}])

    def test_429_then_success_waits_retry_after(self):
        slept = []
        get = FakeGet((429, {}, {"retry-after": "7"}), page([1], total=1))
        self.assertEqual(su.fetch_followed(CANARY_ACCESS, get, sleep=slept.append)[1], 1)
        self.assertEqual(slept, [7])
        self.assertEqual(len(get.calls), 2)
        self.assertEqual(get.calls[0][0], get.calls[1][0])

    def test_429_is_bounded(self):
        slept = []
        get = FakeGet(*[(429, {}, {"retry-after": "2"})] * 10)
        with self.assertRaisesRegex(su.UserAuthError, "429"):
            su.fetch_followed(CANARY_ACCESS, get, sleep=slept.append)
        self.assertEqual(len(get.calls), su.MAX_ATTEMPTS)
        self.assertEqual(slept, [2] * (su.MAX_ATTEMPTS - 1))

    def test_429_long_or_bad_retry_after(self):
        get = FakeGet((429, {}, {"retry-after": "3600"}))
        with self.assertRaisesRegex(su.UserAuthError, "429"):
            su.fetch_followed(CANARY_ACCESS, get, sleep=lambda s: self.fail("must not wait"))
        self.assertEqual(len(get.calls), 1)
        for bad in ["", "abc", "-1", "1.5", "9" * 40]:
            slept = []
            get = FakeGet((429, {}, {"retry-after": bad}), page([1]))
            su.fetch_followed(CANARY_ACCESS, get, sleep=slept.append)
            self.assertEqual(slept, [su.DEFAULT_RETRY_WAIT], bad)

    def test_403_message_is_actionable(self):
        with self.assertRaises(su.UserAuthError) as cm:
            su.fetch_followed(CANARY_ACCESS, FakeGet((403, {"error": {"message": CANARY_ACCESS}}, {})))
        msg = str(cm.exception)
        for word in ("403", "Development Mode", "User Management", "Premium"):
            self.assertIn(word, msg)
        self.assertNotIn(CANARY_ACCESS, msg)

    def test_401_says_log_in_again(self):
        with self.assertRaisesRegex(su.UserAuthError, "401.*login"):
            su.fetch_followed(CANARY_ACCESS, FakeGet((401, {}, {})))

    def test_other_errors_are_redacted_and_carry_no_body(self):
        for status in (400, 500, 502):
            body = {"error": {"status": status, "message": f"boom {CANARY_ACCESS}"}}
            with self.subTest(status=status), self.assertRaises(su.UserAuthError) as cm:
                su.fetch_followed(CANARY_ACCESS, FakeGet((status, body, {})))
            self.assertIn(str(status), str(cm.exception))
            self.assertNotIn(CANARY_ACCESS, str(cm.exception))

    def test_malformed_responses(self):
        bad = [{}, {"artists": []}, {"artists": {"items": "x"}},
               {"artists": {"items": [{"id": "a b", "name": "n"}]}},
               {"artists": {"items": [{"id": aid(1)}]}}, {"artists": {"items": ["x"]}}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(su.UserAuthError):
                su.fetch_followed(CANARY_ACCESS, FakeGet((200, body, {})))

    def test_cursor_that_does_not_advance_or_is_unsafe_fails(self):
        for first, second in [(aid(1), aid(1)), ("a&limit=1", None)]:
            get = FakeGet(page([1], after=first), page([2], after=second))
            with self.subTest(first=first), self.assertRaisesRegex(su.UserAuthError, "cursor"):
                su.fetch_followed(CANARY_ACCESS, get)
        get = FakeGet(*[page([1], after=aid(i)) for i in range(1, 400)])
        with self.assertRaisesRegex(su.UserAuthError, "pages"):
            su.fetch_followed(CANARY_ACCESS, get)
        self.assertEqual(len(get.calls), su.MAX_PAGES)

    def test_network_error_has_no_details(self):
        with mock.patch.object(su.urllib.request.OpenerDirector, "open",
                               side_effect=urllib.error.URLError("dns " + CANARY_ACCESS)):
            with self.assertRaises(su.UserAuthError) as cm:
                su.default_get(su.API_BASE + "/x", {"Authorization": "Bearer " + CANARY_ACCESS}, 1)
        self.assertNotIn(CANARY_ACCESS, str(cm.exception))

    def test_default_get_does_not_follow_redirects(self):
        req = urllib.request.Request("https://api.spotify.com/x", headers={"Authorization": "Bearer t"})
        self.assertIsNone(su._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://evil.example/"))

    def run_followed(self, get, token_side_effect=None):
        lines, out_err, logs = [], io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        patch = mock.patch.object(su, "get_access_token", side_effect=token_side_effect) if token_side_effect \
            else mock.patch.object(su, "get_access_token", return_value=CANARY_ACCESS)
        err = None
        with contextlib.redirect_stdout(out_err), contextlib.redirect_stderr(out_err), patch:
            try:
                su.followed(get, sleep=lambda s: None, out=lines.append)
            except su.UserAuthError as e:
                err = e
        return lines, out_err.getvalue() + logs.getvalue(), err

    def test_command_prints_name_id_and_total_only(self):
        get = FakeGet(page([1, 2], after=aid(2), total=3), page([3], total=3))
        lines, other, err = self.run_followed(get)
        self.assertIsNone(err)
        self.assertEqual(lines, [f"Artist 1\t{aid(1)}", f"Artist 2\t{aid(2)}", f"Artist 3\t{aid(3)}", "total: 3"])
        self.assertEqual(other, "")
        self.assertNotIn(CANARY_ACCESS, "\n".join(lines))

    def test_command_prints_nothing_on_a_partial_failure(self):
        lines, _, err = self.run_followed(FakeGet(page([1], after=aid(1), total=2), (500, {}, {})))
        self.assertIsNotNone(err)
        self.assertEqual(lines, [])

    def test_command_neutralises_control_characters_in_names(self):
        body = (200, {"artists": {"items": [{"id": aid(1), "name": "A\x1b[31m\nB"}], "total": 1}}, {})
        lines, _, _ = self.run_followed(FakeGet(body))
        self.assertEqual(lines[0], f"A?[31m?B\t{aid(1)}")

    def test_command_surfaces_invalid_grant_from_the_token_helper(self):
        real = su.get_access_token

        def boom():
            return real(FakePost(400, {"error": "invalid_grant"}), Path("/nonexistent/.env"))
        with mock.patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "cid", KEY: CANARY_REFRESH}), \
                mock.patch.object(su.tg_check, "load_env"):
            get = FakeGet()
            _, _, err = self.run_followed(get, token_side_effect=boom)
        self.assertRegex(str(err), "invalid_grant.*login")
        self.assertEqual(get.calls, [])

    def test_command_never_logs_in_or_stores(self):
        with mock.patch.object(su, "login") as lg, mock.patch.object(su, "store_refresh_token") as st:
            self.run_followed(FakeGet(page([1])))
        lg.assert_not_called()
        st.assert_not_called()

    def test_main_dispatch(self):
        for cmd, called in (("followed", "followed"), ("login", "login")):
            with mock.patch.object(su.sys, "argv", ["x", cmd]), mock.patch.object(su.tg_check, "load_env"), \
                    mock.patch.object(su, "followed") as f, mock.patch.object(su, "login") as lg:
                su.main()
            self.assertEqual((f.called, lg.called), (called == "followed", called == "login"))
        with mock.patch.object(su.sys, "argv", ["x", "bogus"]), self.assertRaises(SystemExit):
            su.main()

    def test_main_exits_non_zero_with_a_redacted_message(self):
        with mock.patch.object(su.sys, "argv", ["x", "followed"]), mock.patch.object(su.tg_check, "load_env"), \
                mock.patch.object(su, "followed", side_effect=su.UserAuthError("nope")):
            with self.assertRaises(SystemExit) as cm:
                su.main()
        self.assertEqual(cm.exception.code, "ERROR nope")


class Integration(unittest.TestCase):
    def test_required_env_does_not_include_the_refresh_key(self):
        self.assertNotIn(KEY, ingest.REQUIRED_ENV)

    def test_spotdl_does_not_get_the_refresh_token(self):
        self.assertIn(KEY, ingest.SPOTDL_HIDDEN_ENV)


if __name__ == "__main__":
    unittest.main()

"""Spotify user login for a local CLI (Authorization Code with PKCE, loopback redirect) and a token helper.

Usage:
  python ingest/spotify_user.py login
  python ingest/spotify_user.py followed   # read-only: prints "name<TAB>spotify id" per followed artist, then the total

`login` prints an authorization URL; open it in a browser, approve, and the redirect lands on a one-shot listener
on 127.0.0.1:8888. The refresh token is stored as the single key SPOTIFY_USER_REFRESH_TOKEN in the local .env.
`get_access_token()` returns a fresh access token from it. Scope: user-follow-read only. No client secret is used.
Reads SPOTIFY_CLIENT_ID from the environment or ./.env. Tokens are never printed or logged.

Spotify docs relied on (read 2026-10-10):
  https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
  https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens
  https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
"""
import base64
import hashlib
import http.client
import hmac
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

import tg_check
from ingest import redact

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPE = "user-follow-read"
HOST = "127.0.0.1"
PORT = 8888
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{HOST}:{PORT}{CALLBACK_PATH}"
REFRESH_KEY = "SPOTIFY_USER_REFRESH_TOKEN"
LOGIN_TIMEOUT = 300.0  # seconds to wait for the browser redirect
HTTP_TIMEOUT = 15.0
CONN_TIMEOUT = 5.0  # max seconds a single browser connection may stay silent
RELOGIN = "run `python ingest/spotify_user.py login` again"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Allowlist for a value written to .env: base64url/URL-safe token characters. Anything else is rejected, never
# sanitised (a space, quote, '#', newline or non-ASCII byte could change how parse_env reads the line).
_TOKEN_OK = re.compile(r"[A-Za-z0-9._~+/=-]{1,2048}")
_ERROR_CODE = re.compile(r"[a-z_]{1,40}")
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{1,64}")  # base62; also the allowlist for the `after` cursor we send back

API_BASE = "https://api.spotify.com/v1"
PAGE_SIZE = 50  # the maximum for GET /me/following
MAX_PAGES = 200  # 10000 artists; a guard against a cursor that never ends
MAX_ATTEMPTS = 3  # requests per page when Spotify answers 429
RETRY_AFTER_MAX = 30  # seconds; a longer Retry-After is reported instead of waited for
DEFAULT_RETRY_WAIT = 5  # seconds, when a 429 carries no usable Retry-After

# (url, form fields, timeout) -> (HTTP status, parsed JSON object). Injected in tests.
HttpPost = Callable[[str, dict, float], tuple[int, dict]]
# (url, headers, timeout) -> (HTTP status, parsed JSON object, {"retry-after": value or ""}). Injected in tests.
HttpGet = Callable[[str, dict, float], tuple[int, dict, dict]]


class UserAuthError(Exception):
    """Login or refresh failure. The message is redacted and never holds a token, code or response body."""


def _fail(msg: str, secret_values: list[str]) -> UserAuthError:
    return UserAuthError(redact(msg, secret_values))


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 chars from [A-Za-z0-9_-], within the required 43-128


def make_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(client_id: str, challenge: str, state: str) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    return f"{AUTHORIZE_URL}?{q}"


def parse_callback(query: str, expected_state: str) -> str:
    """Return the authorization code from the redirect query, or raise UserAuthError. Accepts exactly
    {code, state} or {error, state}, each once; the state is compared in constant time before anything else."""
    try:
        params = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=10)
    except ValueError:  # more than 10 fields
        raise UserAuthError("callback rejected: too many query parameters") from None
    if any(len(v) != 1 for v in params.values()):
        raise UserAuthError("callback rejected: a query parameter is repeated")
    got = {k: v[0] for k, v in params.items()}
    state = got.get("state")
    if state is None or not hmac.compare_digest(state.encode("utf-8"), expected_state.encode("utf-8")):
        raise UserAuthError("callback rejected: state is missing or does not match")
    if set(got) == {"state", "error"}:
        err = got["error"] if _ERROR_CODE.fullmatch(got["error"]) else "unrecognised"
        raise UserAuthError(f"Spotify denied the login: {err}")
    if set(got) != {"state", "code"} or not got["code"]:
        raise UserAuthError("callback rejected: expected exactly the parameters code and state")
    return got["code"]


def wait_for_callback(expected_state: str, timeout: float = LOGIN_TIMEOUT, port: int = PORT) -> str:
    """One-shot listener on 127.0.0.1 only. Returns the code, or raises (bad callback, timeout, port busy).
    A bad callback to /callback (wrong state, error, extra parameters) aborts the login by design: fail closed.
    The server is single-threaded, so every connection gets a socket timeout (at most CONN_TIMEOUT, and never
    past the overall deadline); an idle connection cannot hold the listener beyond the deadline."""
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = CONN_TIMEOUT  # per-recv socket timeout and, below, the TOTAL time one connection may take;
        # refreshed per request. The socket timeout alone restarts on every byte (a trickling client).
        _cap = None

        def setup(self):
            super().setup()
            self._cap = threading.Timer(self.timeout, self._cut)
            self._cap.daemon = True
            self._cap.start()

        def _cut(self):  # total-time cap hit: shut the socket down so the blocked read ends
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except OSError:  # the client went away or the cap cut the socket: end quietly, no traceback
                self.close_connection = True

        def finish(self):
            if self._cap is not None:
                self._cap.cancel()
            super().finish()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != CALLBACK_PATH:
                self._reply(404, "Not found")
                return
            try:
                result["code"] = parse_callback(url.query, expected_state)
                self._reply(200, "Login complete. You can close this tab.")
            except UserAuthError as e:
                result["error"] = e
                self._reply(400, "Login failed. See the terminal.")

        def _reply(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # no access log: the request line holds the code
            pass

    try:
        server = HTTPServer((HOST, port), Handler)
    except OSError as e:
        raise UserAuthError(f"cannot listen on {HOST}:{port} ({type(e).__name__}); is another process using it?") from None
    deadline = time.monotonic() + timeout
    with server:
        while not result:
            left = deadline - time.monotonic()
            if left <= 0:
                raise UserAuthError(f"timed out after {int(timeout)} s waiting for the browser redirect")
            server.timeout = min(left, 1.0)
            Handler.timeout = max(0.05, min(left, CONN_TIMEOUT))
            server.handle_request()
    if "error" in result:
        raise result["error"]
    return result["code"]


def default_post(url: str, form: dict, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:  # includes timeouts; the class name only, no request details
        raise UserAuthError(f"network error talking to Spotify ({type(e).__name__})") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _token_call(post: HttpPost, form: dict, secret_values: list[str]) -> dict:
    """POST to the token endpoint; return the JSON body of a 200, else raise with the error code only."""
    status, body = post(TOKEN_URL, form, HTTP_TIMEOUT)
    if status == 200 and isinstance(body.get("access_token"), str) and body["access_token"]:
        return body
    err = body.get("error") if isinstance(body.get("error"), str) else ""
    err = err if _ERROR_CODE.fullmatch(err) else "unknown"
    if err == "invalid_grant":
        raise _fail(f"Spotify rejected the grant (invalid_grant): the refresh token or code is expired, revoked "
                    f"or invalid; {RELOGIN}", secret_values)
    raise _fail(f"Spotify token endpoint error: HTTP {status}, error {err}", secret_values)


def _env_secret(value: str) -> str:
    if not _TOKEN_OK.fullmatch(value):
        raise UserAuthError("refusing to store a token with unexpected characters")
    return value


def store_refresh_token(token: str, env_path: Path = ENV_PATH) -> None:
    """Set KEY=token in the .env at env_path, atomically (temp file in the same directory + os.replace), keeping
    the file mode. Every other byte is untouched. Refuses a file parse_env rejects; verifies the result with
    parse_env before replacing. A missing file is created with mode 0600."""
    _env_secret(token)
    real = Path(os.path.realpath(env_path))
    try:
        old = real.read_bytes()
        mode = stat.S_IMODE(real.stat().st_mode)
    except FileNotFoundError:
        old, mode = b"", 0o600
    before = tg_check.parse_env(old)
    line = re.compile(rb"^([ \t]*" + REFRESH_KEY.encode() + rb"=)[^\r\n]*", re.MULTILINE)
    new, n = line.subn(lambda m: m.group(1) + token.encode("ascii"), old, count=1)
    if n == 0:
        sep = b"" if old == b"" or old.endswith(b"\n") else b"\n"
        new = old + sep + f"{REFRESH_KEY}={token}\n".encode("ascii")
    after = tg_check.parse_env(new)
    if after.get(REFRESH_KEY) != token or {k: v for k, v in after.items() if k != REFRESH_KEY} != \
            {k: v for k, v in before.items() if k != REFRESH_KEY}:
        raise UserAuthError("internal check failed: the updated .env would not parse as expected; nothing written")
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _client_id() -> str:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if not cid:
        raise UserAuthError("SPOTIFY_CLIENT_ID is not set")
    return cid


def login(post: HttpPost = default_post, env_path: Path = ENV_PATH, timeout: float = LOGIN_TIMEOUT,
          port: int = PORT, out: Callable[[str], None] = print) -> None:
    cid = _client_id()
    verifier, state = make_verifier(), make_state()
    out("Open this URL in a browser on this machine and approve access (scope: " + SCOPE + "):")
    out(authorize_url(cid, make_challenge(verifier), state))
    out(f"Waiting up to {int(timeout)} s for the redirect to {REDIRECT_URI} ...")
    code = wait_for_callback(state, timeout, port)
    secret_values = [verifier, state, code]
    body = _token_call(post, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                              "client_id": cid, "code_verifier": verifier}, secret_values)
    secret_values.append(body["access_token"])
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise _fail("Spotify returned no refresh token", secret_values)
    secret_values.append(refresh)
    try:
        store_refresh_token(refresh, env_path)
    except (UserAuthError, OSError, tg_check.EnvSyntaxError) as e:
        raise _fail(f"could not store the refresh token: {e}", secret_values) from None
    out(f"Stored the refresh token in .env as {REFRESH_KEY} (not shown). It is valid for about 6 months.")


def get_access_token(post: HttpPost = default_post, env_path: Path = ENV_PATH) -> str:
    """A fresh access token. Raises UserAuthError (with a 'log in again' hint on invalid_grant or when no
    refresh token is stored). A new refresh token in the response replaces the stored one."""
    tg_check.load_env()
    cid = _client_id()
    refresh = os.environ.get(REFRESH_KEY)
    if not refresh:
        raise UserAuthError(f"{REFRESH_KEY} is not set; {RELOGIN}")
    secret_values = [refresh]
    body = _token_call(post, {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid},
                       secret_values)
    access = body["access_token"]
    secret_values.append(access)
    new = body.get("refresh_token")
    if isinstance(new, str) and new and new != refresh:
        secret_values.append(new)
        try:
            store_refresh_token(new, env_path)
        except (UserAuthError, OSError, tg_check.EnvSyntaxError) as e:
            raise _fail(f"could not store the rotated refresh token: {e}", secret_values) from None
        os.environ[REFRESH_KEY] = new
    return access


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: the Authorization header must not leave the API
        return None


def default_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            status, raw, hdrs = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:  # includes timeouts; the class name only, no request details
        raise UserAuthError(f"network error talking to Spotify ({type(e).__name__})") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}, {"retry-after": hdrs.get("Retry-After", "")}


def _clean(text: str) -> str:
    """Printable form of an API string: control characters (terminal escapes, newlines) become '?'."""
    return "".join(c if c.isprintable() else "?" for c in text)


def _get_page(get: HttpGet, sleep: Callable[[float], None], url: str, token: str) -> dict:
    """GET one page with the bearer token. 429 waits Retry-After (at most RETRY_AFTER_MAX s) and retries, up to
    MAX_ATTEMPTS requests in all; every other non-200 raises with a short, fixed message."""
    secret_values = [token]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        status, body, hdrs = get(url, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, HTTP_TIMEOUT)
        if status == 200:
            return body
        if status == 429:
            ra = hdrs.get("retry-after", "")
            wait = int(ra) if ra.isascii() and ra.isdigit() and len(ra) <= 6 else DEFAULT_RETRY_WAIT
            if wait > RETRY_AFTER_MAX:
                raise _fail(f"Spotify rate limit (HTTP 429): asked to wait {wait} s, longer than the {RETRY_AFTER_MAX} s "
                            f"limit; try again later", secret_values)
            if attempt == MAX_ATTEMPTS:
                raise _fail(f"Spotify rate limit (HTTP 429) still active after {MAX_ATTEMPTS} attempts; try again "
                            f"later", secret_values)
            sleep(wait)
            continue
        if status == 401:
            raise _fail(f"Spotify rejected the access token (HTTP 401); {RELOGIN}", secret_values)
        if status == 403:
            raise _fail("Spotify refused the request (HTTP 403). Likely causes: the app is in Development Mode and "
                        "this Spotify account is not added under User Management in the developer dashboard, the app "
                        "owner has no active Premium subscription, or the login did not grant user-follow-read "
                        f"({RELOGIN} if the scope changed)", secret_values)
        raise _fail(f"Spotify API error: HTTP {status}", secret_values)
    raise AssertionError("unreachable")


def fetch_followed(token: str, get: HttpGet = default_get,
                   sleep: Callable[[float], None] = time.sleep) -> tuple[list[dict], int]:
    """All followed artists as ({"id", "name"}, ...) in API order, plus Spotify's `total`. Pages of 50 by the
    `after` cursor (the last artist id). Raises UserAuthError; nothing is returned for a partial result."""
    artists: list[dict] = []
    total = None
    after = None
    for _ in range(MAX_PAGES):
        q = {"type": "artist", "limit": PAGE_SIZE}
        if after is not None:
            q["after"] = after
        page = _get_page(get, sleep, f"{API_BASE}/me/following?{urllib.parse.urlencode(q)}", token)
        block = page.get("artists")
        items = block.get("items") if isinstance(block, dict) else None
        if not isinstance(items, list):
            raise _fail("unexpected response from Spotify: no artists list", [token])
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not isinstance(it.get("name"), str) \
                    or not _SPOTIFY_ID.fullmatch(it["id"]):
                raise _fail("unexpected response from Spotify: malformed artist entry", [token])
            artists.append({"id": it["id"], "name": it["name"]})
        if isinstance(block.get("total"), int) and not isinstance(block.get("total"), bool):
            total = block["total"]
        cursors = block.get("cursors")
        nxt = cursors.get("after") if isinstance(cursors, dict) else None
        if not items or not isinstance(nxt, str) or not nxt:
            return artists, total if total is not None else len(artists)
        if nxt == after or not _SPOTIFY_ID.fullmatch(nxt):
            raise _fail("unexpected response from Spotify: the paging cursor is not usable", [token])
        after = nxt
    raise _fail(f"stopped after {MAX_PAGES} pages: more followed artists than expected", [token])


def followed_artists(token: str, get: HttpGet = default_get,
                     sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """The list for other tools (e.g. #121): [{"id", "name"}, ...]. `token` from get_access_token()."""
    return fetch_followed(token, get, sleep)[0]


def followed(get: HttpGet = default_get, sleep: Callable[[float], None] = time.sleep,
             out: Callable[[str], None] = print) -> None:
    """Print one `name<TAB>id` line per followed artist, then `total: N`. Output only after every page succeeded."""
    token = get_access_token()
    artists, total = fetch_followed(token, get, sleep)
    for a in artists:
        out(f"{_clean(a['name'])}\t{a['id']}")
    out(f"total: {total}")


def main() -> None:
    cmd = sys.argv[1:]
    if cmd not in (["login"], ["followed"]):
        sys.exit(__doc__)
    try:
        tg_check.load_env()
        login() if cmd == ["login"] else followed()
    except (UserAuthError, tg_check.EnvSyntaxError) as e:
        sys.exit(f"ERROR {e}")


if __name__ == "__main__":
    main()

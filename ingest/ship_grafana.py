"""Ship the redacted log of THIS ingest run to Grafana Cloud Loki, best effort (stdlib only).

Called by scripts/run-ingest-local.sh from its finish step, after last-run.json is written:
  ship_grafana.py --log PATH --offset BYTES --last-run PATH [--started-at ISO]

Never fails the run: every problem prints one short local line (class names only, never the URL, user, token
or an exception message) and the exit code is always 0. No retry; the next run is the retry.

Config (local .env through tg_check.load_env, or the environment; key NAMES only here):
  INGEST_GRAFANA_LOKI_URL, INGEST_GRAFANA_LOKI_USER, INGEST_GRAFANA_TOKEN. Any missing: one line, nothing sent.

What leaves the machine: the part of ingest.log written since the run started (bytes from --offset, already
redacted by ingest; redacted again here with the token as an extra secret, control characters dropped, long
lines cut, line count capped) plus one `run_summary` logfmt line built only from allowlisted fields of
last-run.json. Labels: job, host, level.
"""
import argparse
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PUSH_PATH = "/loki/api/v1/push"
HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.grafana\.net")
REQUEST_TIMEOUT_S = 5
OVERALL_DEADLINE_S = 15
MAX_READ_BYTES = 5 * 1024 * 1024
MAX_LINE_CHARS = 4096
MAX_LINES = 2000
KEEP_HEAD = 1000  # when over MAX_LINES: first KEEP_HEAD and last MAX_LINES - KEEP_HEAD - 1 lines
FAILURE_CLASSES = ("none", "partial", "config", "youtube-sign-in", "crash")
COUNT_KEYS = ("added", "skipped", "no_match", "too_big", "failed", "not_attempted")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
LEVEL_RE = re.compile(r"^(?:\S+ run-ingest-local: )?(ERROR|WARN|WARNING)\b")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_COUNT = 10**9


class ShipError(Exception):
    """A reason to skip shipping. The message is a fixed phrase, never a value."""


def say(msg: str) -> None:
    print(f"grafana: {msg}")


def _no_constant(name: str):
    raise ValueError("non-finite number")


def strict_json(data: bytes):
    """json.loads that rejects NaN, Infinity and -Infinity."""
    return json.loads(data.decode("utf-8"), parse_constant=_no_constant)


def validate_url(raw: str) -> str:
    """Return the full push URL, or raise ShipError. https only, host ends in .grafana.net, no userinfo, no
    query or fragment, port 443 or none, path empty or the push path (appended when absent)."""
    try:
        u = urllib.parse.urlsplit(raw.strip())
        host, port = u.hostname, u.port
    except ValueError:
        raise ShipError("invalid url") from None
    if u.scheme != "https":
        raise ShipError("url is not https")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise ShipError("url has userinfo")
    if not host or not HOST_RE.fullmatch(host):
        raise ShipError("host is not a *.grafana.net host")
    if port not in (None, 443):
        raise ShipError("unexpected port")
    if u.query or u.fragment:
        raise ShipError("url has a query or fragment")
    if u.path not in ("", "/", PUSH_PATH):
        raise ShipError("unexpected url path")
    return f"https://{host}{PUSH_PATH}"


def read_config(env=os.environ) -> tuple[str, str, str]:
    url, user, token = (env.get(k, "") for k in
                        ("INGEST_GRAFANA_LOKI_URL", "INGEST_GRAFANA_LOKI_USER", "INGEST_GRAFANA_TOKEN"))
    if not (url and user and token):
        raise ShipError("not configured")
    for v in (user, token):  # they go into an HTTP header
        if CONTROL_RE.search(v) or v != v.strip() or not v.isascii() or ":" in user:
            raise ShipError("invalid credentials format")
    return validate_url(url), user, token


def read_run_text(log_path: str, offset: int) -> tuple[str, int]:
    """The part of the log written since the run started. Returns (text, bytes omitted). Raises ShipError when
    the slice cannot be trusted: the file is shorter than offset (rotated or truncated) or offset is not at the
    start of a line. The wrapper rotates only before `start` and under its lock, so within a run the offset holds."""
    fd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(fd).st_size
        if offset < 0 or size < offset:
            raise ShipError("log shorter than the run offset (rotated?)")
        if offset > 0:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise ShipError("run offset is not at a line start")
        start, omitted = offset, 0
        if size - offset > MAX_READ_BYTES:
            start, omitted = size - MAX_READ_BYTES, size - offset - MAX_READ_BYTES
        f.seek(start)
        data = f.read(MAX_READ_BYTES)
    text = data.decode("utf-8", errors="replace")
    if omitted:  # the first line is cut in the middle: drop it
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text, omitted


def sanitize_lines(text: str, secrets: list[str], redact) -> list[str]:
    text = redact(text, secrets)  # whole text: a token wrapped over two lines is still matched
    lines = []
    for ln in text.splitlines():
        ln = CONTROL_RE.sub("", ln)
        if not ln.strip():
            continue
        if len(ln) > MAX_LINE_CHARS:
            ln = ln[:MAX_LINE_CHARS] + " ...[truncated]"
        lines.append(ln)
    if len(lines) > MAX_LINES:
        tail = MAX_LINES - KEEP_HEAD - 1
        dropped = len(lines) - KEEP_HEAD - tail
        lines = lines[:KEEP_HEAD] + [f"grafana: {dropped} lines omitted (cap {MAX_LINES})"] + lines[-tail:]
    return lines


def level_of(line: str) -> str:
    m = LEVEL_RE.match(line)
    if not m:
        return "info"
    return "error" if m.group(1) == "ERROR" else "warn"


def _count(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_COUNT:
        raise ShipError("last-run.json has an unexpected value")
    return v


def _iso(v) -> datetime:
    if not isinstance(v, str) or not ISO_RE.fullmatch(v):
        raise ShipError("last-run.json has an unexpected value")
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_last_run(path: str, started_at: str | None = None) -> dict:
    """Allowlisted, validated fields of last-run.json: exit_code, failure_class, started, finished, counts.
    Raises ShipError on anything unexpected (parse failure, NaN/Infinity, wrong types): no summary then."""
    try:
        with open(path, "rb") as f:
            d = strict_json(f.read(1024 * 1024))
    except (OSError, ValueError):
        raise ShipError("last-run.json unreadable") from None
    if not isinstance(d, dict):
        raise ShipError("last-run.json is not an object")
    if started_at is not None and d.get("started_at") != started_at:
        raise ShipError("last-run.json is not from this run")  # e.g. the wrapper could not write it
    cls = d.get("failure_class")
    if cls not in FAILURE_CLASSES:
        raise ShipError("last-run.json has an unexpected value")
    out = {"exit_code": _count(d.get("exit_code")), "failure_class": cls,
           "started": _iso(d.get("started_at")), "finished": _iso(d.get("finished_at")), "counts": {}}
    res = d.get("result")
    counts = res.get("counts") if isinstance(res, dict) else None
    if isinstance(counts, dict):
        out["counts"] = {k: _count(counts[k]) for k in COUNT_KEYS if k in counts}
    return out


def summary_line(run: dict) -> str:
    dur = max(0, int((run["finished"] - run["started"]).total_seconds()))
    parts = [f"exit_code={run['exit_code']}", f"failure_class={run['failure_class']}"]
    parts += [f"{k}={run['counts'][k]}" for k in COUNT_KEYS if k in run["counts"]]
    parts.append(f"duration_s={dur}")
    return "run_summary " + " ".join(parts)


def host_label() -> str:
    h = socket.gethostname()
    return h if re.fullmatch(r"[A-Za-z0-9._-]{1,63}", h) else "unknown"


def build_payload(lines: list[str], summary: str | None, summary_level: str, t0_ns: int, now_ns: int,
                  host: str) -> dict:
    """Loki push body. Timestamps are strings (a number gives 400), strictly increasing: t0 + index."""
    by_level: dict[str, list] = {}
    for i, ln in enumerate(lines):
        by_level.setdefault(level_of(ln), []).append([str(t0_ns + i), ln])
    if summary is not None:
        ts = max(now_ns, t0_ns + len(lines))
        by_level.setdefault(summary_level, []).append([str(ts), summary])
    streams = [{"stream": {"job": "cowlic-ingest", "host": host, "level": lvl}, "values": vals}
               for lvl, vals in sorted(by_level.items())]
    return {"streams": streams}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx surfaces as HTTPError; the Authorization header is never forwarded


def make_opener() -> urllib.request.OpenerDirector:
    # No ProxyHandler: the credentials go to the checked host only, never to a proxy named by the environment.
    op = urllib.request.OpenerDirector()
    for h in (urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(), _NoRedirect(),
              urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler()):
        op.add_handler(h)
    return op


def post(url: str, body: bytes, auth: str, opener=None) -> str:
    """One POST, no retry. Returns a class-only result string ("ok (HTTP 204)", "failed (HTTP 429)",
    "failed (URLError)"); never raises, never echoes the URL or an exception message."""
    opener = opener or make_opener()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": auth, "User-Agent": "cowlic-ingest"})
    result: list[str] = []

    def work():
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT_S) as r:
                result.append(f"ok (HTTP {r.status})" if 200 <= r.status < 300 else f"failed (HTTP {r.status})")
        except urllib.error.HTTPError as e:
            result.append(f"failed (HTTP {e.code})")
            e.close()
        except Exception as e:  # noqa: BLE001 - best effort: any failure is reported by class name only
            result.append(f"failed ({type(e).__name__})")

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(OVERALL_DEADLINE_S)
    return result[0] if result else "failed (deadline)"


def ship(log_path: str, offset: int, last_run_path: str, env=os.environ, opener=None, redact=None,
         started_at: str | None = None) -> None:
    url, user, token = read_config(env)
    if redact is None:
        from ingest import redact  # fail closed: without ingest's redaction nothing is sent
    auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    try:
        text, omitted = read_run_text(log_path, offset)
    except (OSError, ShipError) as e:
        say(f"no log lines this run ({e if isinstance(e, ShipError) else type(e).__name__})")
        text, omitted = "", 0
    lines = sanitize_lines(text, [token, auth.split(" ", 1)[1]], redact)
    if omitted:
        lines.insert(0, f"grafana: {omitted} bytes omitted at the start of this run's log")
    summary, level = None, "info"
    try:
        run = parse_last_run(last_run_path, started_at)
        summary, level = summary_line(run), ("info" if run["exit_code"] == 0 else "error")
        t0 = int(run["started"].timestamp()) * 1_000_000_000
    except ShipError as e:
        say(f"no run_summary ({e})")
        t0 = time.time_ns() - len(lines) - 1
    if not lines and summary is None:
        say("nothing to ship")
        return
    payload = build_payload(lines, summary, level, t0, time.time_ns(), host_label())
    body = json.dumps(payload, separators=(",", ":")).encode()
    say(f"loki push {post(url, body, auth, opener)}")


def main(argv: list[str]) -> int:
    try:
        ap = argparse.ArgumentParser()
        ap.add_argument("--log", required=True)
        ap.add_argument("--offset", type=int, required=True)
        ap.add_argument("--last-run", required=True)
        ap.add_argument("--started-at", default=None, help="the run's start (ISO UTC); a last-run.json of another run is ignored")
        args = ap.parse_args(argv)
        try:
            import tg_check
            tg_check.load_env()
        except Exception as e:  # noqa: BLE001 - e.g. EnvSyntaxError; class only, the message could name a key
            say(f"env not loaded ({type(e).__name__})")
        ship(args.log, args.offset, args.last_run, started_at=args.started_at)
    except ShipError as e:
        say(str(e))
    except SystemExit:
        say("bad arguments")
    except BaseException as e:  # noqa: BLE001 - never fail the run
        say(f"failed ({type(e).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

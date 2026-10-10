"""Ship the redacted log of THIS ingest run to Grafana Cloud Loki, best effort (stdlib only).

Called by scripts/run-ingest-local.sh from its finish step, after last-run.json is written:
  ship_grafana.py --log PATH --offset BYTES --last-run PATH [--started-at ISO]

Never fails the run: every problem prints one short local line (class names only, never the URL, user, token
or an exception message) and the exit code is always 0. No retry; the next run is the retry.

Config (local .env through tg_check.load_env, or the environment; key NAMES only here):
  INGEST_GRAFANA_LOKI_URL, INGEST_GRAFANA_LOKI_USER, INGEST_GRAFANA_TOKEN. Any missing: one line, nothing sent.
  Optional run metrics (one Influx line protocol push per run): INGEST_GRAFANA_PROM_URL and INGEST_GRAFANA_PROM_USER
  (the token is shared). Both absent: no metrics. Sent after the log push, only when the log step is configured.

What leaves the machine: the part of ingest.log written since the run started (bytes from --offset, already
redacted by ingest; redacted again here with the token as an extra secret, control characters dropped, long
lines cut, line count capped) plus one `run_summary` logfmt line built only from allowlisted fields of
last-run.json. Labels: job, host, level.
Metrics: numbers only, taken from the same validated fields (see metrics_line); tags job and host, nothing per track.
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PUSH_PATH = "/loki/api/v1/push"
METRICS_PATH = "/api/v1/push/influx/write"
HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.grafana\.net")
REQUEST_TIMEOUT_S = 5
OVERALL_DEADLINE_S = 15
MAX_READ_BYTES = 5 * 1024 * 1024
MAX_LINE_CHARS = 4096
MAX_LINES = 2000
KEEP_HEAD = 1000  # when over MAX_LINES: first KEEP_HEAD and last MAX_LINES - KEEP_HEAD - 1 lines
FAILURE_CLASSES = ("none", "partial", "config", "youtube-sign-in", "crash")
COUNT_KEYS = ("added", "skipped", "no_match", "too_big", "failed", "not_attempted")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # credential validation only
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
MAX_BODY_BYTES = 1024 * 1024
LEVEL_RE = re.compile(r"^(?:\S+ run-ingest-local: )?(ERROR|WARN|WARNING)\b")
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_COUNT = 10**9
JOB_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")  # --job: a Loki label value, allowlisted
MAX_BATCH_BYTES = 256 * 1024  # logs-only follow mode: bytes of log read per push
EXTRA_SECRET_ENV = "COWLIC_EXTRA_SECRET"  # one more value to redact (e.g. a user name); env, never argv


class ShipError(Exception):
    """A reason to skip shipping. The message is a fixed phrase, never a value."""


def say(msg: str) -> None:
    print(f"grafana: {msg}")


def _no_constant(name: str):
    raise ValueError("non-finite number")


def strict_json(data: bytes):
    """json.loads that rejects NaN, Infinity and -Infinity."""
    return json.loads(data.decode("utf-8"), parse_constant=_no_constant)


def validate_url(raw: str, path: str = PUSH_PATH) -> str:
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
    if u.path not in ("", "/", path):
        raise ShipError("unexpected url path")
    return f"https://{host}{path}"


def read_config(env=os.environ) -> tuple[str, str, str]:
    url, user, token = (env.get(k, "") for k in
                        ("INGEST_GRAFANA_LOKI_URL", "INGEST_GRAFANA_LOKI_USER", "INGEST_GRAFANA_TOKEN"))
    if not (url and user and token):
        raise ShipError("not configured")
    for v in (user, token):  # they go into an HTTP header
        if CONTROL_RE.search(v) or v != v.strip() or not v.isascii() or ":" in user:
            raise ShipError("invalid credentials format")
    return validate_url(url), user, token


def read_metrics_config(env, token: str) -> tuple[str, str] | None:
    """(push URL, Basic auth value) for the metrics push, None when metrics are not asked for (both keys absent).
    Raises ShipError for one key alone or a bad value."""
    url, user = env.get("INGEST_GRAFANA_PROM_URL", ""), env.get("INGEST_GRAFANA_PROM_USER", "")
    if not url and not user:
        return None
    if not (url and user):
        raise ShipError("not configured")
    if CONTROL_RE.search(user) or user != user.strip() or not user.isascii() or ":" in user:
        raise ShipError("invalid credentials format")
    return validate_url(url, METRICS_PATH), "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


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


def strip_unsafe(text: str) -> str:
    """Drop control, format (U+200B, U+2060, U+FEFF ...), surrogate and line/paragraph separator characters.
    Only \\n and \\t stay. Must run BEFORE redaction: otherwise they can rebuild a token redaction missed."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in UNSAFE_CATEGORIES)


def clean_text(text: str, secrets: list[str], redact) -> str:
    """Redact, strip unsafe characters, redact again. The first pass removes ANSI sequences (a bare strip would
    leave their parameters behind); the second catches a secret that only appears once the unsafe characters
    between its characters are gone. Used for EVERY text that is sent."""
    return redact(strip_unsafe(redact(text, secrets)), secrets)


def sanitize_lines(text: str, secrets: list[str], redact) -> list[str]:
    text = clean_text(text, secrets, redact)  # whole text: a token wrapped over two lines is still matched
    lines = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if len(ln) > MAX_LINE_CHARS:
            ln = ln[:MAX_LINE_CHARS] + " ...[truncated]"
        lines.append(ln)
    if len(lines) > MAX_LINES:
        tail = MAX_LINES - KEEP_HEAD - 1
        dropped = len(lines) - KEEP_HEAD - tail
        lines = lines[:KEEP_HEAD] + [f"grafana: {dropped} lines omitted (cap {MAX_LINES})"] + lines[-tail:]
    return cap_bytes(lines)


def cap_bytes(lines: list[str]) -> list[str]:
    """Keep the pushed body near MAX_BODY_BYTES: first quarter and the tail of the budget, one marker between."""
    if sum(len(ln.encode()) + 40 for ln in lines) <= MAX_BODY_BYTES:
        return lines
    head_budget, tail_budget = MAX_BODY_BYTES // 4, MAX_BODY_BYTES - MAX_BODY_BYTES // 4 - 1024
    head, used = [], 0
    for ln in lines:
        used += len(ln.encode()) + 40
        if used > head_budget:
            break
        head.append(ln)
    tail, used = [], 0
    for ln in reversed(lines[len(head):]):
        used += len(ln.encode()) + 40
        if used > tail_budget:
            break
        tail.append(ln)
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return head + [f"grafana: {omitted} lines omitted (size cap)"] + tail


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


def metrics_line(run: dict, host: str) -> str:
    """One Influx line protocol line. Measurement cowlic_ingest, tags job and host, integer gauges only: every
    field becomes the series cowlic_ingest_<field>. Values come from parse_last_run (validated ints and a fixed
    failure class list); the host passed the label regex. No timestamp: the server stamps it."""
    dur = max(0, int((run["finished"] - run["started"]).total_seconds()))
    fields = {"last_run_timestamp_seconds": int(run["finished"].timestamp()),
              "success": 1 if run["exit_code"] == 0 else 0, "exit_code": run["exit_code"],
              "duration_seconds": dur}
    fields.update({k: run["counts"][k] for k in COUNT_KEYS if k in run["counts"]})
    for cls in FAILURE_CLASSES[1:]:  # "none" is the absence of the others
        fields["failure_" + cls.replace("-", "_")] = 1 if run["failure_class"] == cls else 0
    return f"cowlic_ingest,job=cowlic-ingest,host={host} " + ",".join(f"{k}={v}i" for k, v in fields.items()) + "\n"


def host_label() -> str:
    h = socket.gethostname()
    return h if re.fullmatch(r"[A-Za-z0-9._-]{1,63}", h) else "unknown"


def build_payload(lines: list[str], summary: str | None, summary_level: str, t0_ns: int, now_ns: int,
                  host: str, job: str = "cowlic-ingest") -> dict:
    """Loki push body. Timestamps are strings (a number gives 400), strictly increasing: t0 + index."""
    by_level: dict[str, list] = {}
    for i, ln in enumerate(lines):
        by_level.setdefault(level_of(ln), []).append([str(t0_ns + i), ln])
    if summary is not None:
        ts = max(now_ns, t0_ns + len(lines))
        by_level.setdefault(summary_level, []).append([str(ts), summary])
    streams = [{"stream": {"job": job, "host": host, "level": lvl}, "values": vals}
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


def post(url: str, body: bytes, auth: str, opener=None, content_type: str = "application/json") -> str:
    """One POST, no retry. Returns a class-only result string ("ok (HTTP 204)", "failed (HTTP 429)",
    "failed (URLError)"); never raises, never echoes the URL or an exception message."""
    opener = opener or make_opener()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type, "Authorization": auth, "User-Agent": "cowlic-ingest"})
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
    summary, level, run = None, "info", None
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
    secrets = [token, auth.split(" ", 1)[1]]
    if summary is not None:  # built from validated fields only, but it goes through the same cleaning
        summary = clean_text(summary, secrets, redact)
    host = host_label()

    def encode(ls, sm):
        body = json.dumps(build_payload(ls, sm, level, t0, time.time_ns(), host), separators=(",", ":")).encode()
        if any(s.encode() in body for s in secrets):  # last guard on the exact bytes sent
            raise ShipError("secret found in payload, not sent")
        return body

    result = post(url, encode(lines, summary), auth, opener)
    status, followup = [f"loki push {result}"], None
    if result in ("failed (HTTP 400)", "failed (HTTP 413)") and lines and summary is not None:
        # the body was probably rejected for its content or size: one small push with just the summary
        followup = f"summary-only push {post(url, encode([], summary), auth, opener)}"
    try:  # optional and isolated: whatever happens here only changes the status line
        sent = ship_metrics(env, token, run, host, opener)
        if sent is not None:
            status.append(f"metrics push {sent}")
    except ShipError as e:
        status.append(f"metrics skipped ({e})")
    except Exception as e:  # noqa: BLE001 - class name only
        status.append(f"metrics failed ({type(e).__name__})")
    say("; ".join(status))  # the wrapper logs only the first line, so everything is on it
    if followup:
        say(followup)


def ship_metrics(env, token: str, run, host: str, opener=None) -> str | None:
    """Push the run's numbers. None: not asked for. Raises ShipError when asked for but impossible."""
    cfg = read_metrics_config(env, token)
    if cfg is None:
        return None
    if run is None:
        raise ShipError("no last-run.json for this run")
    url, auth = cfg
    body = metrics_line(run, host).encode()
    if any(s.encode() in body for s in (token, auth.split(" ", 1)[1])):  # last guard on the exact bytes sent
        raise ShipError("secret found in payload, not sent")
    return post(url, body, auth, opener, content_type="text/plain")


def _job(v: str) -> str:
    if not JOB_RE.fullmatch(v):
        raise argparse.ArgumentTypeError("invalid job label")
    return v


def read_offset(path: str, default: int) -> int:
    """The saved offset of the follow mode; `default` when the file does not exist yet. ShipError when it is
    not a plain integer (never guessed)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(32).strip()
    except FileNotFoundError:
        return default
    except OSError:
        raise ShipError("offset file unreadable") from None
    if not raw.isdigit() or len(raw) > 18:
        raise ShipError("offset file has an unexpected value")
    return int(raw)


def write_offset(path: str, offset: int) -> None:
    """Atomic (temp file in the same directory + rename), mode 0600."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{offset}\n")
    os.replace(tmp, path)


def read_chunk(log_path: str, offset: int, final: bool) -> tuple[str, int]:
    """Follow mode: at most MAX_BATCH_BYTES of the log from `offset`, cut after the last complete line (the
    unfinished last line waits for the next push unless `final`). Returns (text, new offset). A single line longer
    than the batch is shipped cut at the batch size and the rest of that line is skipped (never split over two
    pushes; if its end is not written yet it waits, unless `final`). ShipError when the file is shorter than the offset or the offset is not at a
    line start (rotated or truncated: nothing is guessed)."""
    fd = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(fd).st_size
        if offset < 0 or size < offset:
            raise ShipError("log shorter than the saved offset (rotated?)")
        if offset > 0:
            f.seek(offset - 1)
            if f.read(1) != b"\n":
                raise ShipError("saved offset is not at a line start")
        f.seek(offset)
        data = f.read(MAX_BATCH_BYTES)
        reached_end = len(data) == size - offset
        if final and reached_end:
            return data.decode("utf-8", errors="replace"), offset + len(data)
        cut = data.rfind(b"\n") + 1
        if cut:
            data = data[:cut]
        elif reached_end:
            data = b""  # one unfinished line: wait for its end
        else:
            # A single line longer than the batch: ship its first MAX_BATCH_BYTES (sanitize_lines cuts it further
            # and marks it) and skip to the byte after its newline, so a line is never split over two pushes and
            # the offset stays at a line start. Without a newline yet (still being written): wait, unless final.
            pos = offset + len(data)
            nl = -1
            while nl < 0:
                f.seek(pos)
                more = f.read(65536)
                if not more:
                    break
                i = more.find(b"\n")
                if i >= 0:
                    nl = pos + i
                pos += len(more)
            if nl >= 0:
                return data.decode("utf-8", errors="replace"), nl + 1
            if final:
                return data.decode("utf-8", errors="replace"), pos
            data = b""
    return data.decode("utf-8", errors="replace"), offset + len(data)


def ship_logs_only(log_path: str, offset: int, offset_file: str | None, job: str, final: bool, env=os.environ,
                   opener=None, redact=None, extra_secrets=()) -> None:
    """Logs only, under label `job`: no run_summary, no metrics, no last-run.json. With offset_file the push
    carries the new bytes since the saved offset (bounded batch) and the offset moves on only after the batch was
    accepted or rejected for its content (HTTP 400/413); any other failure leaves it, so the next call retries."""
    url, user, token = read_config(env)
    if redact is None:
        from ingest import redact  # fail closed: without ingest's redaction nothing is sent
    auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    secrets = [token, auth.split(" ", 1)[1], *[s for s in extra_secrets if s]]
    try:
        start = read_offset(offset_file, offset) if offset_file else offset
    except ShipError as e:  # a corrupt offset file must not block shipping for the rest of the run
        say(f"{e}, reset to the start offset")
        start = offset
    try:
        text, new_offset = read_chunk(log_path, start, final)
    except (OSError, ShipError) as e:
        say(f"no log lines ({e if isinstance(e, ShipError) else type(e).__name__})")
        return
    lines = sanitize_lines(text, secrets, redact)
    if not lines:
        if offset_file and new_offset != start:
            write_offset(offset_file, new_offset)
        say("nothing to ship")
        return
    host = host_label()
    body = json.dumps(build_payload(lines, None, "info", time.time_ns() - len(lines), time.time_ns(), host, job),
                      separators=(",", ":")).encode()
    if any(s.encode() in body for s in secrets):  # last guard on the exact bytes sent
        raise ShipError("secret found in payload, not sent")
    result = post(url, body, auth, opener)
    if offset_file and (result.startswith("ok") or result in ("failed (HTTP 400)", "failed (HTTP 413)")):
        write_offset(offset_file, new_offset)
    say(f"loki push {result}")


def filter_stream(inp, out, secrets: list[str], redact) -> None:
    """stdin -> stdout, line by line, through clean_text (redaction, unsafe characters dropped). A line that
    cannot be cleaned is replaced by a fixed marker, never passed on."""
    for raw in inp:
        text = raw.decode("utf-8", errors="replace")
        try:
            cleaned = clean_text(text, secrets, redact)
        except Exception:  # noqa: BLE001 - fail closed per line
            cleaned = "[line withheld: redaction failed]\n"
        if text.endswith("\n") and not cleaned.endswith("\n"):
            cleaned += "\n"
        out.write(cleaned.encode("utf-8"))
        out.flush()


def main(argv: list[str]) -> int:
    try:
        ap = argparse.ArgumentParser()
        ap.add_argument("--log")
        ap.add_argument("--offset", type=int)
        ap.add_argument("--last-run")
        ap.add_argument("--started-at", default=None, help="the run's start (ISO UTC); a last-run.json of another run is ignored")
        ap.add_argument("--job", type=_job, default="cowlic-ingest", help="Loki job label (default cowlic-ingest)")
        ap.add_argument("--logs-only", action="store_true", help="log lines only: no run_summary, no metrics, no last-run.json")
        ap.add_argument("--offset-file", default=None, help="logs-only: keep the shipped offset here (follow mode)")
        ap.add_argument("--final", action="store_true", help="logs-only: also ship an unfinished last line")
        ap.add_argument("--filter", action="store_true", help="filter mode: redact stdin line by line to stdout")
        args = ap.parse_args(argv)
        if args.filter:
            return run_filter()
        if args.log is None or args.offset is None or not (args.logs_only or args.last_run):
            raise SystemExit(2)
        try:
            import tg_check
            tg_check.load_env()
        except Exception as e:  # noqa: BLE001 - e.g. EnvSyntaxError; class only, the message could name a key
            say(f"env not loaded ({type(e).__name__})")
        if args.logs_only:
            if args.job == "cowlic-ingest":  # that label belongs to the nightly run's panels
                raise ShipError("--logs-only needs a --job other than cowlic-ingest")
            ship_logs_only(args.log, args.offset, args.offset_file, args.job, args.final,
                           extra_secrets=[os.environ.get(EXTRA_SECRET_ENV, "")])
        else:
            ship(args.log, args.offset, args.last_run, started_at=args.started_at)
    except ShipError as e:
        say(str(e))
    except SystemExit:
        say("bad arguments")
    except BaseException as e:  # noqa: BLE001 - never fail the run
        say(f"failed ({type(e).__name__})")
    return 0


def run_filter() -> int:
    """Exit code 0 normally; 1 when the redaction cannot be set up (nothing is passed on then)."""
    try:
        import tg_check
        tg_check.load_env()
    except Exception as e:  # noqa: BLE001 - class only
        print(f"grafana: env not loaded ({type(e).__name__})", file=sys.stderr)
    try:
        from ingest import redact
    except Exception as e:  # noqa: BLE001
        print(f"grafana: redaction unavailable ({type(e).__name__})", file=sys.stderr)
        return 1
    secrets = [os.environ.get(k, "") for k in (EXTRA_SECRET_ENV, "INGEST_GRAFANA_TOKEN", "LASTFM_API_KEY")]
    filter_stream(sys.stdin.buffer, sys.stdout.buffer, [s for s in secrets if s], redact)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

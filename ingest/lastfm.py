"""Read-only Last.fm client and a ranking of a user's top artists over the last N years (default 5).

Usage (prints rank, artist name and playcount; the API key is never printed; makes live calls, about 1 per
second, so a 5-year ranking takes about 5 minutes):
  python ingest/lastfm.py top-artists --user <name> [--years 5] [--max 100]
  python ingest/lastfm.py probe-range --user <name>    (3 calls: wide range vs the sum of two adjacent weeks)

Why weekly charts: user.getTopArtists only knows the periods overall/7day/1month/3month/6month/12month (no
from/to) and library.getArtists has no time parameter, so a 5-year window is the sum of the weekly artist charts
inside it: 1 call user.getWeeklyChartList + one user.getWeeklyArtistChart per week (about 261 calls). Only weeks
that lie completely inside [now - years, now] count; a partial week at either edge is left out.

Auth: the read methods used here need only the API key (env LASTFM_API_KEY, loaded from .env by tg_check.load_env,
the same way the other ingest scripts do). The key goes only into the query string of the request, as the API
requires. It never reaches stdout, stderr, an exception or a log: every message goes through redact(), no URL
with its query is ever printed, and a network error shows its class name only.

Last.fm answers errors as JSON (often with HTTP 200): the `error` code is mapped to a clear message. A private
or empty profile is an error, never a silently empty ranking.

Last.fm API terms we follow (https://www.last.fm/api/tos; clause numbers are [R], reported by an earlier research
(#147), to be re-checked against the source before relying on them):
  - non-commercial use only (cl. 3.1 [R]); this is a private, personal library;
  - no use of audio, audiovisual material, images or artwork from Last.fm (cl. 5.1.8 [R]); we use names and
    playcounts only;
  - credit Last.fm, with a link, wherever its data is shown to other people (cl. 4.2.2 / 2.7 [R]);
  - caching follows the HTTP headers of the responses (cl. 4.3 [R]); nothing is cached here;
  - rate pacing: at most 1 call per second, bounded backoff on error 29 (intro: "be reasonable").
Method pages: https://www.last.fm/api/show/user.getWeeklyChartList, .../user.getWeeklyArtistChart.
"""
import argparse
import contextlib
import datetime
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import tg_check
from ingest import redact

API = "https://ws.audioscrobbler.com/2.0/"
READ_METHODS = frozenset({"user.getWeeklyChartList", "user.getWeeklyArtistChart",
                          "artist.getTopAlbums", "album.getInfo"})  # read-only allowlist
HTTP_TIMEOUT = 15.0
MIN_INTERVAL = 1.0  # seconds between two calls
MAX_RETRIES = 3  # retries per call after error 29/11/16, HTTP 5xx or a network error
BACKOFF_BASE = 5.0  # seconds; waits BACKOFF_BASE * 2**attempt
MAX_CALLS = 400  # hard budget of HTTP requests per client (a ranking needs about 1 + 53 per year)
MAX_BODY = 2 * 1024 * 1024  # bytes read from one response; the rest is never read
MAX_YEARS = 20
MAX_ARTISTS = 1000
NAME_MAX = 200
REASON_MAX = 200
WEEKS_PER_YEAR_BOUND = 53

_USER = re.compile(r"[A-Za-z0-9_-]{2,15}")  # Last.fm user names: 2-15 characters of letters, digits, _ and -
_KEY = re.compile(r"[A-Za-z0-9]{16,64}")
_TS = re.compile(r"[0-9]{1,11}")
_COUNT = re.compile(r"[0-9]{1,12}")
_MBID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

ERROR_MESSAGES = {
    6: "user not found (error 6): check the Last.fm user name",
    10: "invalid API key (error 10): check LASTFM_API_KEY",
    17: "login required (error 17): the profile or its listening data is private; make it public",
    26: "API key suspended (error 26)",
    29: "rate limit exceeded (error 29)",
}
RETRYABLE_CODES = frozenset({11, 16, 29})  # service offline, temporary error, rate limit

# (url, timeout) -> (HTTP status, parsed JSON object or {}). Injected in tests; the url holds the key.
Fetch = Callable[[str, float], tuple[int, dict]]


class LastfmError(Exception):
    """Failure talking to Last.fm. The message is redacted: never the key, never a URL with its query.
    `code` is the Last.fm API error code when the failure is one, else None."""

    code: int | None = None


class CallLimitReached(LastfmError):
    """The request budget of the client (or of a `call_budget` block) is used up."""


class ResponseTooLarge(LastfmError):
    """The response body exceeded MAX_BODY; never retried."""


def clean_text(value: str, limit: int = NAME_MAX) -> str:
    """Untrusted text made safe to print: control and format characters removed, whitespace collapsed, cut."""
    text = "".join(c for c in value if unicodedata.category(c)[0] != "C" or c in " \t\n")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


# ---- validation (allowlist; rejects, never rewrites) ----

def parse_user(value: str) -> str:
    if not isinstance(value, str) or not _USER.fullmatch(value):
        raise LastfmError("Last.fm user name must be 2-15 characters of letters, digits, _ or -")
    return value


def api_key_from_env() -> str:
    key = os.environ.get("LASTFM_API_KEY", "")
    if not key:
        raise LastfmError("LASTFM_API_KEY is not set (put it in .env)")
    if not _KEY.fullmatch(key):
        raise LastfmError("LASTFM_API_KEY is set but does not look like a Last.fm API key")
    return key


# ---- HTTP ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: the key must not leave the API host
        return None


_OPEN = urllib.request.build_opener(_NoRedirect).open


def _parse(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError: a deeply nested body that passes the size cap
        return {}
    return body if isinstance(body, dict) else {}


def default_fetch(url: str, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "cowlic-ingest (personal, non-commercial)"})
    try:
        with _OPEN(req, timeout=timeout) as r:
            status, raw = r.status, r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:  # Last.fm also answers errors with 4xx/5xx and a JSON body
        with e:
            status, raw = e.code, e.read(MAX_BODY + 1)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:  # class name only: it can hold the URL
        raise LastfmError(f"network error talking to Last.fm ({type(e).__name__})") from None
    if len(raw) > MAX_BODY:
        raise ResponseTooLarge(f"Last.fm response larger than {MAX_BODY} bytes; stopping")
    return status, _parse(raw)


class LastfmClient:
    """Throttled, bounded, read-only GET client. Counts every HTTP request in `calls`."""

    def __init__(self, api_key: str, fetch: Fetch = default_fetch, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, min_interval: float = MIN_INTERVAL,
                 max_calls: int = MAX_CALLS, backoff_base: float = BACKOFF_BASE,
                 out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)):
        if not isinstance(api_key, str) or not _KEY.fullmatch(api_key):
            raise LastfmError("the Last.fm API key is missing or malformed")
        self._key = api_key
        self._fetch, self._sleep, self._clock = fetch, sleep, clock
        self._interval, self._max_calls, self._backoff, self._out = min_interval, max_calls, backoff_base, out
        self.calls = 0
        self._last: float | None = None

    def _err(self, msg: str, code: int | None = None) -> LastfmError:
        e = LastfmError(redact(msg, [self._key, urllib.parse.quote(self._key)]))
        e.code = code
        return e

    @contextlib.contextmanager
    def call_budget(self, n: int):
        """Allow at most `n` more HTTP requests inside the block (retries count), never more than the client limit."""
        old = self._max_calls
        self._max_calls = min(old, self.calls + n)
        try:
            yield
        finally:
            self._max_calls = old

    def _pace(self) -> None:
        if self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)

    def call(self, method: str, **params: str) -> dict:
        if method not in READ_METHODS:
            raise self._err(f"method {method!r} is not an allowed read-only method")
        query = {"method": method, **params, "api_key": self._key, "format": "json"}
        url = f"{API}?{urllib.parse.urlencode(query)}"
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            if self.calls >= self._max_calls:
                raise CallLimitReached(str(self._err(f"call limit of {self._max_calls} reached; stopping")))
            self._pace()
            self.calls += 1
            try:
                status, body = self._fetch(url, HTTP_TIMEOUT)
            except LastfmError as e:
                if last or isinstance(e, ResponseTooLarge):  # an oversized body is not retried
                    raise self._err(str(e)) from None
                self._wait(attempt, "network error")
                continue
            finally:
                self._last = self._clock()
            if "error" in body:
                code = body["error"]
                if isinstance(code, bool) or not isinstance(code, int):
                    raise self._err(f"malformed Last.fm error answer (HTTP {status})")
                if code in RETRYABLE_CODES:
                    if last:
                        raise self._err(ERROR_MESSAGES.get(code, f"Last.fm error {code}") + f" after {MAX_RETRIES} retries")
                    self._wait(attempt, f"error {code}")
                    continue
                raise self._err(ERROR_MESSAGES.get(code) or self._generic(code, body), code)
            if status >= 500 or status == 429:
                if last:
                    raise self._err(f"Last.fm kept answering HTTP {status} after {MAX_RETRIES} retries")
                self._wait(attempt, f"HTTP {status}")
                continue
            if status != 200:
                raise self._err(f"Last.fm request failed: HTTP {status} on {method}")
            if not body:
                raise self._err(f"Last.fm answered {method} with an empty or non-JSON body")
            return body
        raise self._err("Last.fm request failed after retries")  # unreachable, keeps the contract explicit

    def _wait(self, attempt: int, why: str) -> None:
        secs = self._backoff * 2 ** attempt
        self._out(f"Last.fm {why}, waiting {secs:g} s before retry {attempt + 1}/{MAX_RETRIES}")
        self._sleep(secs)

    def _generic(self, code: int, body: dict) -> str:
        msg = body.get("message")
        reason = clean_text(redact(msg, [self._key]), REASON_MAX) if isinstance(msg, str) else ""
        return f"Last.fm error {code}" + (f": {reason}" if reason else "")


# ---- response parsing ----

def _as_list(value, what: str) -> list:
    if value is None:
        return []
    if isinstance(value, dict):  # JSON-from-XML: a single child is an object, not a list
        return [value]
    if isinstance(value, list):
        return value
    raise LastfmError(f"malformed Last.fm response: `{what}` has an unexpected type")


def parse_chart_list(body: dict) -> list[tuple[int, int]]:
    """Sorted, de-duplicated weekly ranges (from, to) as unix timestamps from a user.getWeeklyChartList answer."""
    root = body.get("weeklychartlist")
    if not isinstance(root, dict):
        raise LastfmError("malformed Last.fm response: `weeklychartlist` is missing")
    ranges = set()
    for c in _as_list(root.get("chart"), "chart"):
        f, t = (c.get("from"), c.get("to")) if isinstance(c, dict) else (None, None)
        if not (isinstance(f, str) and isinstance(t, str) and _TS.fullmatch(f) and _TS.fullmatch(t)) or int(f) >= int(t):
            raise LastfmError("malformed Last.fm response: a weekly chart has an invalid from/to")
        ranges.add((int(f), int(t)))
    ordered = sorted(ranges)
    for (_, prev_to), (nxt_from, _) in zip(ordered, ordered[1:]):
        if nxt_from < prev_to:
            raise LastfmError("malformed Last.fm response: weekly chart ranges overlap")
    return ordered


def parse_artist_chart(body: dict) -> tuple[list[dict], int]:
    """(entries, dropped) from a user.getWeeklyArtistChart answer. An entry is {name, mbid or None, playcount}.
    Entries with a bad name or playcount are dropped and counted, never accepted."""
    root = body.get("weeklyartistchart")
    if not isinstance(root, dict):
        raise LastfmError("malformed Last.fm response: `weeklyartistchart` is missing")
    entries, dropped = [], 0
    for a in _as_list(root.get("artist"), "artist"):
        name = clean_text(a["name"]) if isinstance(a, dict) and isinstance(a.get("name"), str) else ""
        pc = a.get("playcount") if isinstance(a, dict) else None
        if isinstance(pc, int) and not isinstance(pc, bool):
            pc = str(pc)
        if not name or not isinstance(pc, str) or not _COUNT.fullmatch(pc):
            dropped += 1
            continue
        mbid = a.get("mbid")
        entries.append({"name": name, "mbid": mbid.lower() if isinstance(mbid, str) and _MBID.fullmatch(mbid) else None,
                        "playcount": int(pc)})
    return entries, dropped


def _norm_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _key(entry: dict) -> str:
    return f"mbid:{entry['mbid']}" if entry["mbid"] else f"name:{_norm_name(entry['name'])}"


# ---- ranking ----

def _years_ago(now: datetime.datetime, years: int) -> datetime.datetime:
    try:
        return now.replace(year=now.year - years)
    except ValueError:  # 29 February -> 28 February
        return now.replace(year=now.year - years, day=28)


def select_weeks(ranges: list[tuple[int, int]], now: datetime.datetime, years: int) -> list[tuple[int, int]]:
    """The weeks that lie completely inside [now - years, now]."""
    start, end = _years_ago(now, years).timestamp(), now.timestamp()
    return [(f, t) for f, t in ranges if f >= start and t <= end]


def top_artists(client, user: str, years: int = 5, now: datetime.datetime | None = None, max_artists: int = 100,
                out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> list[dict]:
    """Top artists of `user` by playcount over the last `years` years: [{name, mbid or None, playcount}], at most
    `max_artists`, sorted by playcount descending, then name. Artists are summed over the weekly charts, keyed by
    mbid when present, else by normalised name (an artist that has an mbid in some weeks only is not merged)."""
    user = parse_user(user)
    if isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= MAX_YEARS:
        raise LastfmError(f"years must be an integer from 1 to {MAX_YEARS}")
    if isinstance(max_artists, bool) or not isinstance(max_artists, int) or not 1 <= max_artists <= MAX_ARTISTS:
        raise LastfmError(f"max must be an integer from 1 to {MAX_ARTISTS}")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        raise LastfmError("now must be timezone-aware")
    weeks = select_weeks(parse_chart_list(client.call("user.getWeeklyChartList", user=user)), now, years)
    if len(weeks) > years * WEEKS_PER_YEAR_BOUND + 4:
        raise LastfmError(f"{len(weeks)} weekly charts in {years} years: the chart list looks corrupt; stopping")
    if not weeks:
        raise LastfmError("no weekly charts in the window: the profile has no listening history there, or it is private")
    totals: dict[str, dict] = {}
    dropped = 0
    for n, (f, t) in enumerate(weeks, 1):
        entries, d = parse_artist_chart(client.call("user.getWeeklyArtistChart", user=user, **{"from": str(f), "to": str(t)}))
        dropped += d
        for e in entries:
            cur = totals.setdefault(_key(e), {"name": e["name"], "mbid": e["mbid"], "playcount": 0})
            cur["playcount"] += e["playcount"]
        if n % 50 == 0:
            out(f"{n}/{len(weeks)} weekly charts read")
    if dropped:
        out(f"{dropped} chart entries dropped (invalid name or playcount)")
    ranked = sorted(totals.values(), key=lambda a: (-a["playcount"], a["name"].casefold(), a["name"]))
    if not ranked:
        raise LastfmError("the weekly charts hold no artists: the listening history is empty or private")
    return ranked[:max_artists]


def probe_range(client, user: str, now: datetime.datetime | None = None) -> tuple[bool, int, int]:
    """Compare one getWeeklyArtistChart over the two most recent complete adjacent weeks with the sum of the two
    single-week calls. Returns (equal, artists in the wide answer, artists in the sum). 3 calls."""
    user = parse_user(user)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    done = [r for r in parse_chart_list(client.call("user.getWeeklyChartList", user=user)) if r[1] <= now.timestamp()]
    pair = [(a, b) for a, b in zip(done, done[1:]) if a[1] == b[0]]
    if not pair:
        raise LastfmError("no two adjacent weekly charts to compare")
    (a, b) = pair[-1]

    def chart(f: int, t: int) -> dict[str, int]:
        entries, _ = parse_artist_chart(client.call("user.getWeeklyArtistChart", user=user, **{"from": str(f), "to": str(t)}))
        res: dict[str, int] = {}
        for e in entries:
            res[_key(e)] = res.get(_key(e), 0) + e["playcount"]
        return res

    wide, summed = chart(a[0], b[1]), {}
    for part in (chart(*a), chart(*b)):
        for k, v in part.items():
            summed[k] = summed.get(k, 0) + v
    return wide == summed, len(wide), len(summed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Last.fm read-only helpers (live calls, about 1 per second).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    top = sub.add_parser("top-artists", help="top artists over the last N years")
    top.add_argument("--user", required=True)
    top.add_argument("--years", type=int, default=5)
    top.add_argument("--max", type=int, default=100, dest="max_artists")
    probe = sub.add_parser("probe-range", help="check whether one wide range equals the sum of two weeks")
    probe.add_argument("--user", required=True)
    args = ap.parse_args(argv)
    key = ""
    try:
        tg_check.load_env()
        key = api_key_from_env()
        client = LastfmClient(key)
        if args.cmd == "top-artists":
            ranking = top_artists(client, args.user, args.years, max_artists=args.max_artists)
        else:
            equal, n_wide, n_sum = probe_range(client, args.user)
    except (LastfmError, tg_check.EnvSyntaxError) as e:
        print(f"ERROR {redact(str(e), [key])}", file=sys.stderr)
        return 1
    if args.cmd == "top-artists":
        for rank, a in enumerate(ranking, 1):
            print(f"{rank}\t{a['name']}\t{a['playcount']}")
        print(f"{len(ranking)} artists, {client.calls} API calls", file=sys.stderr)
    else:
        print(f"wide range equals the sum of the two weeks: {equal} ({n_wide} vs {n_sum} artists)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

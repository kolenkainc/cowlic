"""Missing-release detector for artists already in the library (#171, contract: docs/bootstrap-ongoing.md 4.3-4.7).

Pure module, nothing here is wired into ingest.py (that is #172). For one artist it lists the newest releases on
YouTube Music (`get_artist_albums(order="Recency")`, FIRST page only, albums and singles, unauthenticated), takes
the SET DIFFERENCE against the album names D1 holds for that artist (`match.normalize` on both sides), fetches the
tracks of every missing release (`get_album`), and returns ingest records for the tracks that `dedupe.drop_stored`
says are not stored yet. No since, no watermark, no lookback_days: the source gives a year only.

The source is injected (`MusicSource`, ytmusicapi.YTMusic fits it); tests use a fake and never go online.

Record ids: `lastfm_tracks.song_id(artist, title)` (`lfm:` + 16 hex of the normalised artist + title). The same song
reached through the Last.fm bootstrap or through this check gets the SAME id, so the D1 `spotify_id UNIQUE` column and
the engine's `existing` set dedupe it by id as well as by the artist + title rule of dedupe.drop_stored. A new prefix
would need SONG_ID and RunState in ingest.py to change, which this ticket must not do.

State: a SEPARATE file (`new-releases-state.json`), see `load_state` / `save_state`. Per artist (key `lfm:` + 16 hex
of the normalised name, the same key as ingest.lastfm_artist_key): browse_id, checked_at (last SUCCESSFUL check),
failures, evaluated (release keys, bounded, oldest evicted). A check is a two-step affair: `check_artist` finds the
records and commits browse_id / checked_at / failures and the releases with nothing new to add; the releases that
have new records are added to `evaluated` by `mark_outcomes` once the caller knows what became of every track.

Bounds: at most MAX_ARTIST_REQUESTS (5) requests per artist per night (1 get_artist + 2 get_artist_albums, each of up
to 2 requests), +1 search when the browse id is not cached, +1 get_album per release looked at (`max_releases`); at
least MIN_INTERVAL_S between source calls; the first refusal (HTTP 429/403, bot check) ends the check without a retry
and without any bypass (#62).

Logging: counts and the hashed artist key only. No artist names, no release titles, no URLs, no exception text (a
source error is reduced to its class name) reach `out`.

Known limitations (documented, tested): a compilation, deluxe or regional edition the owner already holds under a
different title looks MISSING (the `evaluated` set bounds the cost, drop_stored bounds the duplicates); tracks of a
release whose artists do not include the artist are dropped; a release whose tracks all fail stays unevaluated.
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Protocol

import dedupe
import lastfm_tracks
from match import normalize

STATE_VERSION = 1
STATE_FILE_NAME = "new-releases-state.json"
STATE_MAX_BYTES = 5 * 1024 * 1024
STATE_MAX_ARTISTS = 100_000
MAX_EVALUATED = 50          # release keys remembered per artist
RECHECK_DAYS = 7
MIN_INTERVAL_S = 1.0        # between two source calls
MAX_ARTIST_REQUESTS = 5     # 1 get_artist + 2 get_artist_albums of up to 2 requests each
SEARCH_REQUESTS = 1
MAX_RELEASES = 5            # get_album calls per artist per night
YEAR_MARGIN = 1             # a release older than (newest stored album year - margin) is skipped
MAX_TEXT_LEN = 300
MAX_LISTED = 100            # raw list entries looked at per list
MAX_PER_KIND = 20           # releases kept per list (albums, singles)
MAX_TRACKS = 200            # tracks read per album
MAX_TRACK_NO = 999
MAX_DURATION_S = 36000
ARTIST_ORDER = "Recency"
ID_PREFIX = "lfm:"

# statuses of one check
OK, NOT_DUE, UNRESOLVED, AMBIGUOUS, FAILED, REFUSED, UNUSABLE = (
    "ok", "not_due", "unresolved", "ambiguous", "failed", "refused", "unusable")
# what became of a track (the caller reports these to `mark_outcomes`)
HANDLED_STATUSES = frozenset({"stored", "present", "dropped", "no_match", "too_big"})

_ARTIST_KEY = re.compile(r"lfm:[0-9a-f]{16}")
_RELEASE_KEY = re.compile(r"[0-9a-f]{16}:(?:[0-9]{4})?")
_BROWSE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_ALBUM_ID = re.compile(r"MPRE[A-Za-z0-9_-]{1,100}")
_PARAMS = re.compile(r"[A-Za-z0-9_%=+/-]{1,2000}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REFUSAL_CODE = re.compile(r"\b(?:429|403)\b")
_REFUSAL_WORDS = ("too many requests", "rate limit", "sign in to confirm", "not a bot", "unusual traffic", "captcha",
                  "forbidden")


class NewReleasesError(Exception):
    """Base class; the text never carries a name, a title or a URL."""


class NewReleasesStateError(NewReleasesError):
    """The state file is unreadable or malformed. It is never silently replaced by an empty one."""


class SourceError(NewReleasesError):
    """A source call failed; only the exception class name is kept."""


class SourceRefused(SourceError):
    """The source refused (rate limit, HTTP 429/403, bot check). The caller stops the phase: no retry, no bypass."""


class BudgetExceeded(NewReleasesError):
    """The per-artist request budget is used up."""


# ---- the injected source ----

class MusicSource(Protocol):
    """The four ytmusicapi.YTMusic methods this module uses (the real client satisfies it as it is)."""

    def search(self, query: str, filter: str | None = None, limit: int = 20) -> list: ...

    def get_artist(self, channelId: str) -> dict: ...

    def get_artist_albums(self, channelId: str, params: str, limit: int | None = 100, order: str | None = None) -> list: ...

    def get_album(self, browseId: str) -> dict: ...


class Pacer:
    """At least `interval` seconds between two source calls; clock and sleep are injected for tests."""

    def __init__(self, interval: float = MIN_INTERVAL_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.interval:
            self.sleep(self.interval - (now - self._last))
            now = self.clock()
        self._last = now


class GuardedSource:
    """One artist's view of the source: paces every call, charges the request budget (get_artist_albums with an order
    costs 2 requests), reduces any error to a class name and recognises a refusal."""

    def __init__(self, source: MusicSource, pacer: Pacer, budget: int):
        self._source, self._pacer, self.budget, self.requests = source, pacer, budget, 0

    def _call(self, cost: int, fn: Callable, *args, **kwargs):
        if self.requests + cost > self.budget:
            raise BudgetExceeded(f"request budget of {self.budget} per artist is used up")
        self.requests += cost
        self._pacer.wait()
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - reduced to a class name, never re-raised with its text
            if _is_refusal(e):
                raise SourceRefused(f"source refused the request ({type(e).__name__})") from None
            raise SourceError(f"source call failed ({type(e).__name__})") from None

    def search(self, query: str):
        return self._call(SEARCH_REQUESTS, self._source.search, query, filter="artists", limit=10)

    def get_artist(self, browse_id: str):
        return self._call(1, self._source.get_artist, browse_id)

    def get_artist_albums(self, channel_id: str, params: str):
        # limit=1: the first page only, never a continuation; the sort continuation is the 2nd request
        return self._call(2, self._source.get_artist_albums, channel_id, params, limit=1, order=ARTIST_ORDER)

    def get_album(self, browse_id: str):
        return self._call(1, self._source.get_album, browse_id)


def _is_refusal(exc: BaseException) -> bool:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        if v in (403, 429):
            return True
    text = str(exc).lower()
    return bool(_REFUSAL_CODE.search(text)) or any(w in text for w in _REFUSAL_WORDS)


# ---- keys and untrusted text ----

def artist_key(name: str) -> str | None:
    """`lfm:` + 16 hex of the normalised name: the key of ingest.lastfm_artist_key (a test keeps the two equal).
    None when nothing is left after normalising."""
    n = normalize(name) if isinstance(name, str) and len(name) <= MAX_TEXT_LEN else ""
    if not n:
        return None
    return ID_PREFIX + hashlib.sha256(("lastfm artist\x1f" + n.strip().casefold()).encode()).hexdigest()[:16]


def release_key(title: str, year: int | None) -> str | None:
    """16 hex of the normalised release title + `:` + the year or nothing. None for a title with nothing left."""
    n = normalize(title)
    if not n:
        return None
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:16] + ":" + ("" if year is None else str(year))


def clean_text(value, max_len: int = MAX_TEXT_LEN) -> str | None:
    """Untrusted text from the source: control and format characters become spaces, whitespace is collapsed. None
    when it is not a string, is empty, or is longer than max_len (rejected, never cut)."""
    if not isinstance(value, str) or len(value) > max_len * 4:
        return None
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in value)
    text = " ".join(text.split())
    return text if text and len(text) <= max_len else None


def _year(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) == 4:
        n = int(value)
    else:
        return None
    return n if 1000 <= n <= 2999 else None


def _int(value, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lo <= value <= hi else None


# ---- state ----

def empty_state() -> dict:
    return {"version": STATE_VERSION, "artists": {}}


def validate_state(obj) -> dict:
    """The state object when it is well-formed, else NewReleasesStateError. Strict: unknown keys are refused."""
    if not isinstance(obj, dict) or set(obj) != {"version", "artists"}:
        raise NewReleasesStateError("new-releases state: top level must have exactly `version` and `artists`")
    if obj["version"] != STATE_VERSION or isinstance(obj["version"], bool):
        raise NewReleasesStateError(f"new-releases state: unsupported version (expected {STATE_VERSION})")
    artists = obj["artists"]
    if not isinstance(artists, dict) or len(artists) > STATE_MAX_ARTISTS:
        raise NewReleasesStateError("new-releases state: `artists` must be an object of bounded size")
    for key, entry in artists.items():
        if not isinstance(key, str) or not _ARTIST_KEY.fullmatch(key):
            raise NewReleasesStateError("new-releases state: an artist key is not `lfm:` + 16 hex")
        if not isinstance(entry, dict) or not set(entry) <= {"browse_id", "checked_at", "failures", "evaluated"}:
            raise NewReleasesStateError(f"new-releases state: entry {key} has a wrong shape")
        b = entry.get("browse_id")
        if "browse_id" in entry and not (isinstance(b, str) and _BROWSE_ID.fullmatch(b)):
            raise NewReleasesStateError(f"new-releases state: entry {key} has a malformed browse_id")
        c = entry.get("checked_at")
        if "checked_at" in entry and not (isinstance(c, str) and _DATE.fullmatch(c) and _parse_date(c) is not None):
            raise NewReleasesStateError(f"new-releases state: entry {key} has a malformed checked_at")
        f = entry.get("failures", 0)
        if isinstance(f, bool) or not isinstance(f, int) or not 0 <= f <= 10**9:
            raise NewReleasesStateError(f"new-releases state: entry {key} has a malformed failures count")
        ev = entry.get("evaluated", [])
        if (not isinstance(ev, list) or len(ev) > MAX_EVALUATED or len(set(ev)) != len(ev)
                or not all(isinstance(k, str) and _RELEASE_KEY.fullmatch(k) for k in ev)):
            raise NewReleasesStateError(f"new-releases state: entry {key} has a malformed evaluated list")
    return obj


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def load_state(path: Path) -> dict:
    """The validated state; a missing file is a fresh state. An unreadable, oversized or malformed file raises
    NewReleasesStateError (it is never treated as empty: that would forget every evaluated release)."""
    path = Path(path)
    try:
        if not path.exists():
            return empty_state()
        if not path.is_file() or path.stat().st_size > STATE_MAX_BYTES:
            raise NewReleasesStateError(f"new-releases state file {path.name} is not a regular file of sane size")
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise NewReleasesStateError(f"cannot read new-releases state file {path.name} ({type(e).__name__})") from None
    except UnicodeDecodeError:
        raise NewReleasesStateError(f"new-releases state file {path.name} is not valid UTF-8") from None
    try:
        obj = json.loads(raw)
    except ValueError:
        raise NewReleasesStateError(
            f"new-releases state file {path.name} is not valid JSON; fix or delete it (it is not replaced silently)") from None
    return validate_state(obj)


def save_state(path: Path, state: dict) -> None:
    """Validate, then temp file in the same directory (fsynced) + rename; file 0600, directory 0700 when created.
    The previous file is untouched on any failure; raises NewReleasesStateError / OSError."""
    validate_state(state)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def add_evaluated(evaluated: list, key: str, cap: int = MAX_EVALUATED) -> list:
    """`evaluated` (oldest first) with `key` appended; a key already present moves to the end; the oldest are evicted
    beyond `cap`. Returns a new list."""
    out = [k for k in evaluated if k != key] + [key]
    return out[-cap:]


# ---- which artists are due ----

def is_due(entry: dict | None, today: date, recheck_days: int = RECHECK_DAYS) -> bool:
    """Never checked, or the last successful check is `recheck_days` or more days ago. A checked_at in the future
    (clock skew, a wrong file) counts as due, so it cannot block an artist for good."""
    c = entry.get("checked_at") if entry else None
    if c is None:
        return True
    d = _parse_date(c)
    return d is None or d > today or (today - d).days >= recheck_days


def select_due(names: Iterable[str], state: dict, today: date, max_artists: int,
               recheck_days: int = RECHECK_DAYS) -> tuple[list[str], int]:
    """(up to max_artists due names, number of due names). Never-checked first, then the oldest check; ties keep
    the input order; a name that normalises to nothing or repeats an earlier one is ignored."""
    seen: set[str] = set()
    due: list[tuple[str, int, str]] = []
    for i, name in enumerate(names):
        key = artist_key(name)
        if key is None or key in seen:
            continue
        seen.add(key)
        entry = state["artists"].get(key)
        if is_due(entry, today, recheck_days):
            due.append(((entry or {}).get("checked_at") or "", i, name))
    due.sort(key=lambda t: (t[0], t[1]))
    return [n for _, _, n in due[:max(0, max_artists)]], len(due)


# ---- artist link ----

def resolve_browse_id(results, name: str) -> tuple[str, str | None]:
    """(OK, browse id) for exactly one distinct artist whose normalised name equals `name`; (AMBIGUOUS, None) for
    two or more distinct ids with that name; (UNRESOLVED, None) for none. Only the name equality counts: a near
    match is not accepted."""
    want = normalize(name)
    ids: list[str] = []
    if isinstance(results, list):
        for item in results[:MAX_LISTED]:
            if not isinstance(item, dict):
                continue
            rtype = item.get("resultType")
            if rtype is not None and rtype != "artist":
                continue
            title = clean_text(item.get("artist") if item.get("artist") is not None else item.get("title"))
            bid = item.get("browseId")
            if title is None or normalize(title) != want or not isinstance(bid, str) or not _BROWSE_ID.fullmatch(bid):
                continue
            if bid not in ids:
                ids.append(bid)
    if not ids:
        return UNRESOLVED, None
    return (OK, ids[0]) if len(ids) == 1 else (AMBIGUOUS, None)


# ---- releases ----

@dataclass(frozen=True)
class Release:
    title: str
    year: int | None
    browse_id: str
    kind: str  # "album" or "single"
    key: str


def parse_releases(raw, kind: str) -> list[Release]:
    """The usable releases of one list (get_artist_albums / the inline results of get_artist), source order kept.
    Entries without a clean title, a valid album id or a key are dropped; at most MAX_LISTED entries are looked at and
    MAX_PER_KIND kept; a release listed twice (same key) is kept once."""
    out: list[Release] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_LISTED]:
        if len(out) >= MAX_PER_KIND:
            break
        if not isinstance(item, dict):
            continue
        title, bid = clean_text(item.get("title")), item.get("browseId")
        if title is None or not isinstance(bid, str) or not _ALBUM_ID.fullmatch(bid):
            continue
        year = _year(item.get("year"))
        key = release_key(title, year)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(Release(title, year, bid, kind, key))
    return out


@dataclass
class Missing:
    candidates: list[Release]
    listed: int
    in_library: int = 0     # normalised title found among the stored albums
    evaluated: int = 0      # key in the evaluated set
    skipped_old: int = 0    # clearly older than the newest stored album year


def find_missing(releases: list[Release], stored_albums: Iterable[tuple], evaluated: Iterable[str],
                 year_margin: int = YEAR_MARGIN) -> Missing:
    """The SET DIFFERENCE on (artist, normalised title). `stored_albums` are (album name, year or None) of THIS
    artist in D1. A release is missing when its normalised title is not among them and its key is not evaluated.
    The year is a skip filter only (older than the newest stored year minus the margin: skipped); a release without
    a year is always a candidate. Dated candidates come first, newest first; undated ones follow."""
    names: set[str] = set()
    newest = None
    for row in stored_albums:
        name, yr = row
        if isinstance(name, str) and len(name) <= MAX_TEXT_LEN and normalize(name):
            names.add(normalize(name))
            y = _year(yr)
            if y is not None and (newest is None or y > newest):
                newest = y
    done = set(evaluated)
    res = Missing([], len(releases))
    for rel in releases:
        if normalize(rel.title) in names:
            res.in_library += 1
        elif rel.key in done:
            res.evaluated += 1
        elif rel.year is not None and newest is not None and rel.year < newest - year_margin:
            res.skipped_old += 1
        else:
            res.candidates.append(rel)
    dated = sorted((r for r in res.candidates if r.year is not None), key=lambda r: -r.year)  # stable
    res.candidates = dated + [r for r in res.candidates if r.year is None]
    return res


# ---- records ----

def build_records(artist: str, release: Release, album) -> list[dict] | None:
    """Ingest records of a `get_album` answer (the shape of lastfm_tracks.make_record, duration in whole seconds or
    None, never invented). None when the answer is not a dict with a track list. A track without a clean title or id,
    or whose listed artists do not include `artist`, is dropped; a repeated id keeps the first."""
    if not isinstance(album, dict) or not isinstance(album.get("tracks"), list):
        return None
    album_name = clean_text(album.get("title")) or release.title
    year = _year(album.get("year")) or release.year
    base = normalize(artist)
    records: list[dict] = []
    seen: set[str] = set()
    for t in album["tracks"][:MAX_TRACKS]:
        if not isinstance(t, dict):
            continue
        title = clean_text(t.get("title"))
        sid = lastfm_tracks.song_id(artist, title) if title else None
        if sid is None or sid in seen:
            continue
        others = []
        for a in (t.get("artists") if isinstance(t.get("artists"), list) else [])[:20]:
            n = clean_text(a.get("name")) if isinstance(a, dict) else None
            if n:
                others.append(n)
        if others and base not in {normalize(n) for n in others}:
            continue
        seen.add(sid)
        extra: list[str] = []
        for n in others:
            if normalize(n) not in {base, *(normalize(e) for e in extra)} and normalize(n):
                extra.append(n)
        explicit = t.get("isExplicit")
        records.append({
            "song_id": sid, "name": title, "artists": [artist, *extra], "album_artist": artist,
            "album_name": album_name, "year": year, "track_number": _int(t.get("trackNumber"), 1, MAX_TRACK_NO),
            "disc_number": 1, "duration": _int(t.get("duration_seconds"), 1, MAX_DURATION_S),
            "url": None, "cover_url": None, "album_type": "album" if release.kind == "album" else "single",
            "release_date": None, "explicit": explicit if isinstance(explicit, bool) else None})
    return records


# ---- one artist ----

@dataclass
class PendingRelease:
    key: str
    song_ids: tuple[str, ...]


@dataclass
class ArtistCheck:
    key: str | None
    status: str
    records: list = field(default_factory=list)       # new tracks, the same dict shape as lastfm_tracks records
    pending: list[PendingRelease] = field(default_factory=list)  # releases with new records, not yet evaluated
    listed: int = 0          # releases on the first page(s)
    missing: int = 0         # candidates after the set difference (before the max_releases cap)
    deferred: int = 0        # candidates left for a later night by the max_releases cap
    in_library: int = 0
    evaluated_hits: int = 0
    skipped_old: int = 0
    dropped_stored: int = 0  # tracks dropped by dedupe.drop_stored
    requests: int = 0


def log_line(res: ArtistCheck) -> str:
    """One line, counts and the hashed key only (it is shipped to Grafana)."""
    return (f"new releases {res.key}: {res.status}, {res.listed} listed, {res.in_library} in library, "
            f"{res.evaluated_hits} evaluated, {res.skipped_old} too old, {res.missing} missing, {res.deferred} deferred, "
            f"{len(res.records)} new tracks, {res.dropped_stored} already stored, {res.requests} requests")


def _fail(state: dict, key: str, res: ArtistCheck, status: str, browse_id: str | None) -> ArtistCheck:
    entry = state["artists"].setdefault(key, {})
    if browse_id:
        entry["browse_id"] = browse_id  # a resolved link stays valid whatever happened next
    entry["failures"] = entry.get("failures", 0) + 1
    res.status = status
    return res


def check_artist(source: MusicSource, artist: str, state: dict, stored_rows: Iterable[tuple],
                 stored_albums: Iterable[tuple] = (), *, today: date, pacer: Pacer | None = None,
                 max_releases: int = MAX_RELEASES, recheck_days: int = RECHECK_DAYS, force: bool = False,
                 out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> ArtistCheck:
    """The missing releases of one artist as ingest records (ArtistCheck.records).

    `stored_rows`: (title, primary artist, duration_s) of every stored track (dedupe.drop_stored). `stored_albums`:
    (album name, year) of THIS artist in D1 (the caller filters in Python from one read-only query). `state` is the
    validated state dict, modified in place only as follows: a resolved browse_id is cached; a successful check
    sets checked_at and resets failures and evaluates the releases with nothing new; a failed or refused check only
    raises `failures`. The caller saves the state (`save_state`). Not due artists return NOT_DUE without any call.
    An unresolved or ambiguous artist is not a failure: it counts as checked (retried after `recheck_days`) and
    costs no further calls. On REFUSED the caller must stop the whole phase."""
    key = artist_key(artist)
    res = ArtistCheck(key=key, status=OK)
    if key is None:
        res.status = UNUSABLE
        out(log_line(res))
        return res
    entry = state["artists"].get(key)
    if not force and not is_due(entry, today, recheck_days):
        res.status = NOT_DUE
        out(log_line(res))
        return res
    browse_id = (entry or {}).get("browse_id")
    budget = MAX_ARTIST_REQUESTS + (0 if browse_id else SEARCH_REQUESTS) + max(0, max_releases)
    src = GuardedSource(source, pacer or Pacer(), budget)
    try:
        _check(src, artist, key, state, stored_rows, stored_albums, today, max_releases, browse_id, res)
    except SourceRefused:
        _fail(state, key, res, REFUSED, state["artists"].get(key, {}).get("browse_id"))
    except NewReleasesError:  # SourceError, BudgetExceeded: the text names no artist
        _fail(state, key, res, FAILED, state["artists"].get(key, {}).get("browse_id"))
    res.requests = src.requests  # records and pending are set only at the very end of a successful check
    out(log_line(res))
    return res


def _check(src: GuardedSource, artist: str, key: str, state: dict, stored_rows, stored_albums, today: date,
           max_releases: int, browse_id: str | None, res: ArtistCheck) -> None:
    entry = state["artists"].get(key) or {}
    if not browse_id:
        status, browse_id = resolve_browse_id(src.search(artist), artist)
        if browse_id is None:  # no exact match / several artists with the name: skipped, not a failure
            state["artists"][key] = {**entry, "checked_at": today.isoformat(), "failures": 0}
            res.status = status
            return
        entry = state["artists"].setdefault(key, {})
        entry["browse_id"] = browse_id  # kept even when a later call fails
    page = src.get_artist(browse_id)
    releases = _list_releases(src, page)
    missing = find_missing(releases, stored_albums, entry.get("evaluated", []))
    res.listed, res.in_library, res.evaluated_hits, res.skipped_old = (
        missing.listed, missing.in_library, missing.evaluated, missing.skipped_old)
    res.missing = len(missing.candidates)
    chosen = missing.candidates[:max(0, max_releases)]
    res.deferred = len(missing.candidates) - len(chosen)
    rows = list(stored_rows)
    evaluated = list(entry.get("evaluated", []))
    pending: list[PendingRelease] = []
    records: list[dict] = []
    taken: set[str] = set()
    dropped = 0
    for rel in chosen:
        recs = build_records(artist, rel, src.get_album(rel.browse_id))
        if recs is None:
            raise SourceError("source call failed (malformed album answer)")
        new = dedupe.drop_stored(recs, rows).new
        dropped += len(recs) - len(new)
        fresh = [r for r in new if r["song_id"] not in taken]  # the same song on an album and a single: once
        taken.update(r["song_id"] for r in fresh)
        records.extend(fresh)
        if new:
            pending.append(PendingRelease(rel.key, tuple(r["song_id"] for r in new)))
        else:  # every track already stored (or none usable): handled now
            evaluated = add_evaluated(evaluated, rel.key)
    entry = state["artists"].setdefault(key, {})  # commit only after every call succeeded
    entry.update({"browse_id": browse_id, "checked_at": today.isoformat(), "failures": 0, "evaluated": evaluated})
    res.records, res.pending, res.dropped_stored = records, pending, dropped


def _list_releases(src: GuardedSource, page) -> list[Release]:
    if not isinstance(page, dict):
        raise SourceError("source call failed (malformed artist answer)")
    releases: list[Release] = []
    seen: set[str] = set()
    for section, kind in (("albums", "album"), ("singles", "single")):
        sect = page.get(section)
        if not isinstance(sect, dict):
            continue
        channel, params = sect.get("browseId"), sect.get("params")
        if (isinstance(channel, str) and _BROWSE_ID.fullmatch(channel)
                and isinstance(params, str) and _PARAMS.fullmatch(params)):
            raw = src.get_artist_albums(channel, params)  # the full list, newest first
        else:
            raw = sect.get("results")  # few releases: the page lists them all, no extra call
        for rel in parse_releases(raw, kind):
            if rel.key not in seen:
                seen.add(rel.key)
                releases.append(rel)
    return releases


def mark_outcomes(state: dict, res: ArtistCheck, statuses: dict[str, str]) -> int:
    """After the tracks were handled: add to `evaluated` every pending release whose tracks ALL have a status in
    HANDLED_STATUSES (`statuses` maps song_id to one of them, or to "failed"). A release with a failed track, a track
    without a status (cut by a cap or the time limit) is left out and retried. Returns the number added."""
    entry = state["artists"].get(res.key) if res.key else None
    if entry is None:
        return 0
    added = 0
    evaluated = list(entry.get("evaluated", []))
    for p in res.pending:
        if all(statuses.get(s) in HANDLED_STATUSES for s in p.song_ids):
            evaluated = add_evaluated(evaluated, p.key)
            added += 1
    entry["evaluated"] = evaluated
    return added


@dataclass
class Totals:
    """Counts for the run line (no names)."""
    due: int = 0
    checked: int = 0
    with_new: int = 0
    added: int = 0
    failed: int = 0
    skipped: int = 0

    def add(self, res: ArtistCheck) -> None:
        if res.status == NOT_DUE:
            return
        if res.status in (FAILED, REFUSED):
            self.failed += 1
        elif res.status == OK:
            self.checked += 1
            self.with_new += bool(res.records)
        else:
            self.skipped += 1

    def line(self) -> str:
        return (f"new releases: {self.due} due, {self.checked} checked, {self.with_new} with new, {self.added} added, "
                f"{self.failed} failed, {self.skipped} skipped")

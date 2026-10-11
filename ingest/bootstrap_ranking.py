"""The stored Last.fm ranking of the Release 3 bootstrap (#167): compute it once, keep it, define the priority order.

API for bootstrap.py (#168) and the nightly new-release phase (#172); no CLI here:
  compute(client, user, years, max_artists, path=None, refresh=False) -> ranking   (live Last.fm calls via `client`)
  save(path, ranking) / load(path) / read(path)
  order(artists), build(artists, years, max_artists), age_days(ranking)

File: ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/bootstrap-ranking.json (default_path()), directory 0700, file
0600, written by atomic rename (temp file + fsync + rename), so a reader sees the old or the new file, never half a
file. SHARED ONE-WAY: only the bootstrap writes it (compute/save), the nightly run only reads it.

  {"version": 1, "computed_at": "2026-10-11T18:00:00Z", "years": 5, "max_artists": 100,
   "artists": [{"name": "Example Band", "playcount": 1234, "mbid": "<uuid, only when Last.fm gave one>"}]}

The list is stored in priority order: playcount descending, ties by `match.normalize(name)` ascending (`order` is
the one place that defines it). No Last.fm user name, no key, no URL. The file is the owner's listening data: it
stays in the local state directory (never in the repo), and nothing here prints or logs a name; messages carry
counts and reasons only. Everything read from the file is untrusted and validated strictly (exact keys, types,
ranges, size cap, sorted order, unique keys); a bad file is an error with a reason, never an empty list.

Refresh policy: the ranking is computed once; `compute` refuses while the file exists unless refresh=True (the
owner's explicit `--refresh`), and a failed compute leaves the old file untouched. Nothing recomputes on age:
`age_days` only lets callers show how old the ranking is.

Sizing for the caller: `compute` makes 1 + weeks calls, at most 1 + years * 53 + 4 (lastfm.top_artists); the
client must allow that many (LastfmClient max_calls defaults to MAX_CALLS = 400, enough for years <= 7); a client
with a smaller budget fails with CallLimitReached before anything is written. The per-artist enumeration budget
(lastfm_tracks.CALL_BUDGET) is separate and is the caller's to size.
"""
import datetime
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Callable

import ingest
import lastfm
from lastfm import _MBID, clean_text
from match import normalize

VERSION = 1
FILE_NAME = "bootstrap-ranking.json"
MAX_BYTES = 1024 * 1024  # a bigger file is not ours: the biggest valid one (1000 artists) is about 0.3 MB
MAX_PLAYCOUNT = 10 ** 12  # lastfm._COUNT allows 12 digits
_TOP_KEYS = frozenset({"version", "computed_at", "years", "max_artists", "artists"})
_ENTRY_KEYS = frozenset({"name", "playcount", "mbid"})
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class RankingError(Exception):
    """A missing, unreadable, corrupt or unsupported ranking, or a refused write. The message names a reason, never
    an artist name or the file content. `kind` is one of: missing, invalid, version, exists, unwritable."""

    def __init__(self, message: str, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def default_path() -> Path:
    """Next to ingest-state.json and the log: ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/bootstrap-ranking.json."""
    return ingest.default_state_path().with_name(FILE_NAME)


# ---- order and build ----

def order(artists: list[dict]) -> list[dict]:
    """A new list in priority order: playcount descending, ties by match.normalize(name) ascending (then the name
    itself, so equal input in any order gives one result)."""
    return sorted(artists, key=lambda a: (-a["playcount"], normalize(a["name"]), a["name"]))


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _check_name(name) -> str:
    if not isinstance(name, str) or not name or clean_text(name) != name:
        raise RankingError("an artist name is empty, not text, or not clean")
    return name


def _stored(artist: dict) -> dict:
    """The stored form of one artist: {name, playcount} plus mbid only when valid (never an empty string)."""
    entry = {"name": _check_name(clean_text(artist["name"]) if isinstance(artist.get("name"), str) else None),
             "playcount": artist.get("playcount")}
    if not _int_in(entry["playcount"], 0, MAX_PLAYCOUNT):
        raise RankingError("an artist playcount is not an integer in range")
    mbid = artist.get("mbid")
    if isinstance(mbid, str) and _MBID.fullmatch(mbid):
        entry["mbid"] = mbid.lower()
    return entry


def _with_rank(stored: list[dict]) -> list[dict]:
    """{name, key, playcount, mbid when present, rank} per artist; `key` is the engine's lastfm_artist_key."""
    return [{**a, "key": ingest.lastfm_artist_key(a["name"]), "rank": n} for n, a in enumerate(stored, 1)]


def build(artists: list[dict], years: int, max_artists: int,
          now: datetime.datetime | None = None) -> dict:
    """The ranking from `lastfm.top_artists` output ([{name, mbid or None, playcount}]): ordered, entries with the
    same engine key dropped after the first (the higher ranked), cut to `max_artists`, each with key and rank."""
    if not _int_in(years, 1, lastfm.MAX_YEARS) or not _int_in(max_artists, 1, lastfm.MAX_ARTISTS):
        raise RankingError("years or max_artists is out of range")
    if not isinstance(artists, list):
        raise RankingError("the artist list is not a list")
    seen, kept = set(), []
    for a in order([_stored(x) for x in artists if isinstance(x, dict)]):
        key = ingest.lastfm_artist_key(a["name"])
        if key not in seen:
            seen.add(key)
            kept.append(a)
    if not kept:
        raise RankingError("the ranking would be empty")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        raise RankingError("now must be timezone-aware")
    return {"version": VERSION, "computed_at": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "years": years, "max_artists": max_artists, "artists": _with_rank(kept[:max_artists])}


def age_days(ranking: dict, now: datetime.datetime | None = None) -> int:
    """Whole days since `computed_at` (0 for a clock that went back)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    done = datetime.datetime.strptime(ranking["computed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return max(0, (now - done).days)


# ---- validation, load, save ----

def _validate(doc, *, strict_order: bool = True) -> dict:
    """The ranking (with key and rank per artist) from a parsed file, or RankingError. Rejects, never rewrites."""
    if not isinstance(doc, dict):
        raise RankingError("the ranking file is not a JSON object")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise RankingError("the ranking file has no valid version")
    if version != VERSION:
        raise RankingError(f"the ranking file has version {version}; this code supports only {VERSION}", "version")
    if set(doc) != _TOP_KEYS:
        raise RankingError("the ranking file has missing or unknown fields")
    stamp = doc["computed_at"]
    try:
        if not (isinstance(stamp, str) and _TIME.fullmatch(stamp)):
            raise ValueError
        datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise RankingError("computed_at is not a UTC timestamp") from None
    if not _int_in(doc["years"], 1, lastfm.MAX_YEARS) or not _int_in(doc["max_artists"], 1, lastfm.MAX_ARTISTS):
        raise RankingError("years or max_artists is out of range")
    artists = doc["artists"]
    if not isinstance(artists, list) or not 1 <= len(artists) <= doc["max_artists"]:
        raise RankingError("the artist list is missing, empty or longer than max_artists")
    stored, keys = [], set()
    for a in artists:
        if not isinstance(a, dict) or not {"name", "playcount"} <= set(a) <= _ENTRY_KEYS:
            raise RankingError("an artist entry has missing or unknown fields")
        _check_name(a["name"])
        if not _int_in(a["playcount"], 0, MAX_PLAYCOUNT):
            raise RankingError("an artist playcount is not an integer in range")
        if "mbid" in a and not (isinstance(a["mbid"], str) and _MBID.fullmatch(a["mbid"]) and a["mbid"] == a["mbid"].lower()):
            raise RankingError("an artist mbid is not a lowercase MusicBrainz id")
        key = ingest.lastfm_artist_key(a["name"])
        if key in keys:
            raise RankingError("two artists share one key")
        keys.add(key)
        stored.append(dict(a))
    if strict_order and stored != order(stored):
        raise RankingError("the artists are not in priority order")
    return {"version": VERSION, "computed_at": stamp, "years": doc["years"], "max_artists": doc["max_artists"],
            "artists": _with_rank(stored)}


def _reject_constant(name: str):
    raise ValueError("non-finite number")


def load(path: Path | str | None = None) -> dict:
    """The validated ranking. Raises RankingError (kind missing, invalid or version) for a missing, oversized,
    unreadable, corrupt or unknown-version file: never an empty list."""
    path = Path(path) if path else default_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise RankingError("no ranking file", "missing") from None
    except OSError as e:
        raise RankingError(f"the ranking file cannot be opened ({type(e).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RankingError("the ranking path is not a regular file")
        if st.st_size > MAX_BYTES:
            raise RankingError(f"the ranking file is larger than {MAX_BYTES} bytes")
        with os.fdopen(fd, "rb") as f:  # takes over the descriptor and closes it
            fd = -1
            raw = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise RankingError(f"the ranking file cannot be read ({type(e).__name__})") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_BYTES:
        raise RankingError(f"the ranking file is larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise RankingError("the ranking file is not valid JSON") from None
    return _validate(doc)


def read(path: Path | str | None = None) -> tuple[dict | None, str]:
    """For callers that treat any problem as "no ranking": (ranking, "") or (None, reason). The reason names no
    artist and is safe to log."""
    try:
        return load(path), ""
    except RankingError as e:
        return None, str(e)


def save(path: Path | str | None, ranking: dict) -> None:
    """Write `ranking` (from build/compute/load) atomically: only name, playcount and mbid per artist are stored.
    The data is validated first, so a file `load` would refuse is never written; on failure the old file stays.
    Raises RankingError."""
    path = Path(path) if path else default_path()
    if not isinstance(ranking, dict) or not isinstance(ranking.get("artists"), list):
        raise RankingError("the ranking to save is malformed")
    doc = {"version": ranking.get("version"), "computed_at": ranking.get("computed_at"), "years": ranking.get("years"),
           "max_artists": ranking.get("max_artists"),
           "artists": [{k: a[k] for k in ("name", "playcount", "mbid") if k in a} if isinstance(a, dict) else a
                       for a in ranking["artists"]]}
    _validate(doc)
    if len(json.dumps(doc)) > MAX_BYTES:
        raise RankingError(f"the ranking is larger than {MAX_BYTES} bytes")
    try:
        ingest.atomic_write_json(path, doc)
    except OSError as e:
        raise RankingError(f"the ranking file cannot be written ({type(e).__name__})", "unwritable") from None


# ---- compute ----

def compute(client, user: str, years: int, max_artists: int, path: Path | str | None = None, refresh: bool = False,
            now: datetime.datetime | None = None,
            out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> dict:
    """Compute the ranking with `lastfm.top_artists` (live calls through `client`), store it and return it.
    Refused (RankingError kind exists) while the file exists unless refresh=True, before any call is made. Any
    failure (Last.fm, validation, disk) raises and leaves an existing file untouched."""
    path = Path(path) if path else default_path()
    if path.exists() and not refresh:
        raise RankingError("a ranking already exists; pass refresh=True (--refresh) to replace it", "exists")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    top = lastfm.top_artists(client, user, years, now=now, max_artists=max_artists, out=out)
    ranking = build(top, years, max_artists, now)
    save(path, ranking)
    out(f"ranking stored: {len(ranking['artists'])} artists")
    return ranking

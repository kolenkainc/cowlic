"""The logic of `bootstrap.py covers` (#250): backfill the cover of albums stored without one. Design: docs/cover-art.md
section 7, runbook: docs/bootstrap.md section 11.

For each album with `cover_file_id IS NULL` (one stored track per album) it runs the SAME matcher as ingest
(match.find_match: 1 to 3 YouTube Music searches), applies the cover guard of #248 (match.cover_url_from_match: the
matched album name and artist must fit the stored ones), fetches the thumbnail through the hardened route of #248
(ingest.fetch_cover with the host allowlist), uploads it as a Telegram photo (ingest's upload_cover) and writes

    UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL

(parameterized, idempotent: a cover another process stored meanwhile is never replaced). No audio is downloaded.

This module holds no lock, no 21:10 guard and no command line: bootstrap.py owns those (it imports this module, never
the other way round). Nothing here reads the Last.fm ranking or the bootstrap progress file. The only local file is the
cooldown file `cover-backfill.json` (album id -> result word and date; ids are hashes, no names).

Never bypassed: a YouTube sign-in challenge ends the run (YouTubeSignInRequired, exit 3 in bootstrap.py): no cookies,
proxies, PO-token tricks or client rotation (#62). Logs carry counts and class words only, never a name or a title.
"""
import datetime
import json
import os
import random
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ingest
import match

STATE_FILE = "cover-backfill.json"
STATE_VERSION = 1
STATE_MAX_BYTES = 8 * 1024 * 1024
STATE_MAX_ENTRIES = 200_000
RETRY_DAYS_RANGE = (0, 3650)
DEFAULT_RETRY_DAYS = 30  # same horizon as the engine's no_match cooldown: a missing match rarely appears within weeks
COOLDOWN_RESULTS = ("no_match", "mismatch")
PACE_SECONDS = 1.5  # at least this long between two YouTube Music calls ...
PACE_JITTER_SECONDS = 1.0  # ... plus a random 0..1 s
MAX_CONSECUTIVE_FAILURES = 3  # a D1, Telegram or network outage must not burn the whole cap
ALBUM_ID = re.compile(r"[0-9a-f]{16}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# One stored track per cover-less album (the smallest track id). Read-only and parameterless; the unknown album is
# filtered in read_todo (so the dry count can report how many there are).
ALBUMS_SQL = ("SELECT al.id AS album_id, al.name AS album, ar.name AS artist, t.title AS title, t.duration_s AS duration_s "
              "FROM albums al JOIN artists ar ON ar.id = al.artist_id "
              "JOIN tracks t ON t.id = (SELECT MIN(t2.id) FROM tracks t2 WHERE t2.album_id = al.id) "
              "WHERE al.cover_file_id IS NULL ORDER BY al.id")
ARTISTS_COUNT_SQL = "SELECT COUNT(*) AS n FROM artists WHERE image_file_id IS NULL"
UPDATE_SQL = "UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL"

OUTCOMES = ("covered", "already", "no_match", "album_mismatch", "artist_mismatch", "no_cover_in_match", "failed")
# the guard's reason words (match.cover_url_from_match) -> the outcome word and the cooldown result
_GUARD = {"album_mismatch": ("album_mismatch", "mismatch"), "artist_mismatch": ("artist_mismatch", "mismatch"),
          "no_thumbnail": ("no_cover_in_match", "mismatch"), "no_match_album": ("no_cover_in_match", "mismatch"),
          "no_album": ("no_cover_in_match", "mismatch")}


class CoverStateError(Exception):
    """The cooldown file must not be used. The message is a reason without any file content."""


# ---- the cooldown file ----

def _iso_date(value) -> bool:
    if not (isinstance(value, str) and _DATE.fullmatch(value)):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _read_state_doc(path: Path):
    """The parsed JSON or None when the file does not exist; CoverStateError for anything unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CoverStateError(f"cannot be opened ({type(e).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CoverStateError("not a regular file")
        if st.st_size > STATE_MAX_BYTES:
            raise CoverStateError(f"larger than {STATE_MAX_BYTES} bytes")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(STATE_MAX_BYTES + 1)
    except OSError as e:
        raise CoverStateError(f"cannot be read ({type(e).__name__})") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > STATE_MAX_BYTES:
        raise CoverStateError(f"larger than {STATE_MAX_BYTES} bytes")

    def reject(_name):
        raise ValueError("non-finite number")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject)
    except (ValueError, RecursionError):
        raise CoverStateError("not valid JSON") from None


class CoverState:
    """cover-backfill.json: {"version": 1, "albums": {<album id>: {"result": "no_match"|"mismatch", "on": "YYYY-MM-DD"}}}.
    Written atomically (ingest.atomic_write_json: 0600, directory 0700) after every change, so a killed run keeps what it
    learned. Strict on read: any unknown field or malformed entry refuses the file (it is never silently dropped)."""

    def __init__(self, path: Path):
        self.path = path
        self.albums: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path) -> "CoverState":
        doc = _read_state_doc(path)
        state = cls(path)
        if doc is None:
            return state
        if not isinstance(doc, dict) or set(doc) != {"version", "albums"}:
            raise CoverStateError("missing or unknown fields")
        if isinstance(doc["version"], bool) or doc["version"] != STATE_VERSION:
            raise CoverStateError("unknown version")
        albums = doc["albums"]
        if not isinstance(albums, dict) or len(albums) > STATE_MAX_ENTRIES:
            raise CoverStateError("albums is not an object of a sane size")
        for key, e in albums.items():
            ok = (isinstance(key, str) and ALBUM_ID.fullmatch(key) and isinstance(e, dict) and set(e) == {"result", "on"}
                  and e["result"] in COOLDOWN_RESULTS and _iso_date(e["on"]))
            if not ok:
                raise CoverStateError("an entry is malformed")
        state.albums = {k: dict(e) for k, e in albums.items()}
        return state

    def cooled(self, album_id: str, today: datetime.date, retry_days: int) -> bool:
        """True while the album must not be retried: its entry is younger than `retry_days` (0 disables the cooldown)."""
        e = self.albums.get(album_id)
        if e is None or retry_days <= 0:
            return False
        return today < datetime.date.fromisoformat(e["on"]) + datetime.timedelta(days=retry_days)

    def record(self, album_id: str, result: str, today: datetime.date) -> None:
        self.albums[album_id] = {"result": result, "on": today.isoformat()}

    def forget(self, album_id: str) -> bool:
        return self.albums.pop(album_id, None) is not None

    def save(self) -> None:
        ingest.atomic_write_json(self.path, {"version": STATE_VERSION, "albums": self.albums})  # OSError on failure


# ---- the work list ----

@dataclass
class Todo:
    album_id: str
    album: str
    artist: str
    title: str
    duration_s: int | None


def read_todo(query: Callable[[str, list | None], list]) -> tuple[list[Todo], dict]:
    """(albums to work in id order, {"unknown": n, "bad": n}). One read-only D1 query; rows are validated, never trusted.
    An album named like UNKNOWN_ALBUM is never filled (no real album to compare with)."""
    out: list[Todo] = []
    skipped = {"unknown": 0, "bad": 0}
    for r in query(ALBUMS_SQL, None):
        aid = r.get("album_id") if isinstance(r, dict) else None
        texts = [r.get(k) if isinstance(r, dict) else None for k in ("album", "artist", "title")]
        if not (isinstance(aid, str) and ALBUM_ID.fullmatch(aid) and all(isinstance(x, str) and x.strip() for x in texts)):
            skipped["bad"] += 1
            continue
        album, artist, title = texts
        if album.strip().casefold() == ingest.UNKNOWN_ALBUM.casefold():
            skipped["unknown"] += 1
            continue
        dur = r.get("duration_s")
        out.append(Todo(aid, album, artist, title, dur if isinstance(dur, int) and not isinstance(dur, bool) and dur > 0 else None))
    return out, skipped


# ---- pacing and retries for the YouTube Music search ----

class PacedSearch:
    """Wraps a YTMusic client for match.find_match: at least PACE_SECONDS (+ jitter) between two search calls, bounded
    retries (ingest.Throttle) for transient errors only (HTTP 429/5xx, network), and a sign-in challenge in an error text
    becomes YouTubeSignInRequired, which is never retried. `sleep`, `clock` and `rng` are injectable for tests."""

    def __init__(self, ytm, throttle: "ingest.Throttle", pace: float = PACE_SECONDS, jitter: float = PACE_JITTER_SECONDS,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 rng: Callable[[], float] | None = None):
        self.ytm, self.throttle, self.pace, self.jitter = ytm, throttle, pace, jitter
        self.sleep, self.clock, self.rng = sleep or time.sleep, clock or time.monotonic, rng or random.random
        self.last_end: float | None = None
        self.calls = 0

    def _pause(self) -> None:
        if self.last_end is None:
            return
        wait = self.pace + self.rng() * self.jitter - (self.clock() - self.last_end)
        if wait > 0:
            self.sleep(wait)

    def _once(self, query, filter, limit):
        try:
            return self.ytm.search(query, filter=filter, limit=limit)
        except ingest.YouTubeSignInRequired:
            raise
        except Exception as e:  # re-raised unless it is the bot check
            if ingest.SIGN_IN_CHALLENGE.search(str(e)):
                raise ingest.YouTubeSignInRequired("YouTube asks to sign in (bot check)") from None
            raise

    def search(self, query, filter=None, limit=None):
        self._pause()
        self.calls += 1
        try:
            return self.throttle.call(lambda: self._once(query, filter, limit), ingest.transient_search,
                                      "YouTube Music search")
        finally:
            self.last_end = self.clock()


# ---- one album, then the run ----

@dataclass
class CoverBackends:
    """Everything outside the process. Tests pass fakes."""
    find_match: Callable[[Todo], object | None]       # the shared matcher on the stored track; None = no match
    upload_cover: Callable[[dict, Path], str]         # ingest's upload_cover: {"cover_url": ...} -> Telegram file_id
    d1_write: Callable[[str, list], object]           # one parameterized statement -> D1 result


def real_backends() -> CoverBackends:
    """The real network side: the hardened #248 fetch and the sendPhoto path (ingest.real_deps(...).upload_cover), the
    shared matcher through a paced client, D1 writes through ingest.d1_batch."""
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    paced = PacedSearch(ytm, ingest.Throttle(0.0, 0.0, ingest.DEFAULT_ATTEMPTS))
    deps = ingest.real_deps(ytm)  # no throttle: only upload_cover is used, the search is paced by PacedSearch
    return CoverBackends(
        find_match=lambda t: match.find_match(paced, t.title, t.artist, t.album, t.duration_s),
        upload_cover=deps.upload_cover,
        d1_write=lambda sql, params: ingest.d1_batch([(sql, params)]))


def changed_rows(result) -> int | None:
    """meta.changes of the D1 answer for one statement, None when the shape is unexpected."""
    try:
        n = result[0]["meta"]["changes"]
    except (KeyError, IndexError, TypeError):
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def attempt(todo: Todo, backends: CoverBackends) -> str:
    """One album: the outcome word. Raises YouTubeSignInRequired (never caught here); every other expected failure is the
    word `failed` (the exception type is logged by the caller, never its text)."""
    try:
        m = backends.find_match(todo)
        if m is None:
            return "no_match"
        url, why = match.cover_url_from_match(todo.album, todo.artist, m)
        if url is None:
            return _GUARD.get(why, ("no_cover_in_match", "mismatch"))[0]
        with tempfile.TemporaryDirectory() as tmp:
            file_id = backends.upload_cover({"cover_url": url, ingest.COVER_FROM_MATCH: True}, Path(tmp))
        if not isinstance(file_id, str) or not file_id:
            raise ingest.IngestError("no file id")
        changes = changed_rows(backends.d1_write(UPDATE_SQL, [file_id, todo.album_id]))
        return "already" if changes == 0 else "covered"
    except ingest.YouTubeSignInRequired:
        raise
    except (ingest.IngestError, OSError, KeyError, ValueError, TypeError) as e:
        ingest.log(f"WARN covers: attempt failed ({type(e).__name__})")
        return "failed"


def cooldown_result(outcome: str) -> str | None:
    if outcome == "no_match":
        return "no_match"
    if outcome in ("album_mismatch", "artist_mismatch", "no_cover_in_match"):
        return "mismatch"
    return None


@dataclass
class CoverRun:
    counts: dict = field(default_factory=lambda: {k: 0 for k in OUTCOMES})
    cooldown: int = 0        # albums skipped because their entry is still young
    not_attempted: int = 0   # albums left when a limit (or a stop) ended the run
    stop_reason: str = "done"
    interrupted: bool = False  # sign-in challenge
    unsaved: bool = False      # the cooldown file could not be written

    @property
    def failed(self) -> bool:
        return bool(self.counts["failed"] or self.unsaved)


def run_backfill(todo: list[Todo], backends: CoverBackends, state: CoverState, limits: "ingest.RunLimits",
                 today: Callable[[], datetime.date], retry_days: int) -> CoverRun:
    """Works `todo` in order until a cap, the time limit, a sign-in challenge or too many failures in a row. --max-new
    counts albums ATTEMPTED (every attempt costs search calls); cooldown albums are skipped and not counted."""
    run = CoverRun()
    consecutive = 0
    for i, t in enumerate(todo):
        if state.cooled(t.album_id, today(), retry_days):
            run.cooldown += 1
            continue
        reason = limits.exhausted()
        if reason:
            run.stop_reason = reason
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days) for x in todo[i:])
            break
        limits.attempted += 1
        try:
            outcome = attempt(t, backends)
        except ingest.YouTubeSignInRequired:
            run.interrupted, run.stop_reason = True, "sign-in"
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days) for x in todo[i:])
            break
        run.counts[outcome] += 1
        ingest.log(f"covers: album {limits.attempted}: {outcome}")
        consecutive = consecutive + 1 if outcome == "failed" else 0
        changed = False
        cooled_as = cooldown_result(outcome)
        if cooled_as:
            state.record(t.album_id, cooled_as, today())
            changed = True
        elif outcome in ("covered", "already"):
            changed = state.forget(t.album_id)
        if changed:
            try:
                state.save()
            except OSError as e:
                run.unsaved = True
                ingest.log(f"WARN covers: could not write {STATE_FILE} ({type(e).__name__}); the next album retries")
            else:
                run.unsaved = False
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            run.stop_reason = "failed"
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days) for x in todo[i + 1:])
            break
    return run


# ---- the dry count ----

def count_lines(query: Callable[[str, list | None], list], state: CoverState, today: datetime.date,
                retry_days: int) -> list[str]:
    """`covers --count`: D1 reads only (the same album list as the run, one count of artists). No YouTube, no Telegram,
    no write, no file. Lines hold counts only. The artist half reports `not counted` while the column does not exist."""
    todo, skipped = read_todo(query)
    cooled = sum(state.cooled(t.album_id, today, retry_days) for t in todo)
    lines = [f"covers: albums without a cover: {len(todo) + skipped['unknown'] + skipped['bad']} "
             f"(to try: {len(todo) - cooled}, in cooldown: {cooled}, unknown album: {skipped['unknown']}, "
             f"unreadable rows: {skipped['bad']}; retry cooldown {retry_days} days)"]
    try:
        rows = query(ARTISTS_COUNT_SQL, None)
        n = rows[0]["n"]
        if isinstance(n, bool) or not isinstance(n, int):
            raise ingest.IngestError("unexpected artist count")
        lines.append(f"covers: artists without an image: {n} (in cooldown: 0)")
    except ingest.IngestError as e:
        if "no such column" not in str(e):
            raise
        lines.append("covers: artists without an image: not counted yet (the artists.image_file_id column does not exist)")
    return lines

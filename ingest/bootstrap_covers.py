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

Second source and plan (#352): `covers --dry-run` classifies every album by rule (the strict guard, the narrow name
normalisation of match.albums_match(narrow=True), an album-level search with a tracklist check) and writes only a PLAN file
(cover-plan.json, private state directory); `covers --apply-plan FILE` applies exactly the planned accepts, and only when the
owner set "approved": true in the file by hand. Neither the dry run nor a refused apply writes D1 or Telegram.

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
COOLDOWN_RESULTS = ("no_match", "mismatch", "failed")
DEFAULT_FAILED_DAYS = 3  # a failure (thumbnail 404, non-JPEG, Telegram refusing the image, ...) rests shorter than a mismatch
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
ARTISTS_COLUMN_SQL = "SELECT 1 AS present FROM pragma_table_info('artists') WHERE name = 'image_file_id'"
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

    FILE_KEY = "albums"   # the object's key in the file; the artist-photo state (#253) is a subclass with "artists"
    ID_RE = ALBUM_ID

    def __init__(self, path: Path):
        self.path = path
        self.albums: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path) -> "CoverState":
        doc = _read_state_doc(path)
        state = cls(path)
        if doc is None:
            return state
        if not isinstance(doc, dict) or set(doc) != {"version", cls.FILE_KEY}:
            raise CoverStateError("missing or unknown fields")
        if isinstance(doc["version"], bool) or doc["version"] != STATE_VERSION:
            raise CoverStateError("unknown version")
        albums = doc[cls.FILE_KEY]
        if not isinstance(albums, dict) or len(albums) > STATE_MAX_ENTRIES:
            raise CoverStateError("albums is not an object of a sane size")
        for key, e in albums.items():
            ok = (isinstance(key, str) and cls.ID_RE.fullmatch(key) and isinstance(e, dict) and set(e) == {"result", "on"}
                  and e["result"] in COOLDOWN_RESULTS and _iso_date(e["on"]))
            if not ok:
                raise CoverStateError("an entry is malformed")
        state.albums = {k: dict(e) for k, e in albums.items()}
        return state

    def cooled(self, album_id: str, today: datetime.date, retry_days: int, failed_days: int = DEFAULT_FAILED_DAYS) -> bool:
        """True while the album must not be retried: its entry is younger than `retry_days` (`failed_days` for a failed
        attempt); 0 disables that cooldown."""
        e = self.albums.get(album_id)
        days = failed_days if e is not None and e["result"] == "failed" else retry_days
        if e is None or days <= 0:
            return False
        return today < datetime.date.fromisoformat(e["on"]) + datetime.timedelta(days=days)

    def record(self, album_id: str, result: str, today: datetime.date) -> None:
        self.albums[album_id] = {"result": result, "on": today.isoformat()}

    def forget(self, album_id: str) -> bool:
        return self.albums.pop(album_id, None) is not None

    def save(self) -> None:
        ingest.atomic_write_json(self.path, {"version": STATE_VERSION, self.FILE_KEY: self.albums})  # OSError on failure


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

    def _once(self, call):
        try:
            return call()
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
            return self.throttle.call(lambda: self._once(lambda: self.ytm.search(query, filter=filter, limit=limit)),
                                      ingest.transient_search, "YouTube Music search")
        finally:
            self.last_end = self.clock()

    def get_album(self, browse_id):
        """The tracklist call of the album-level source: paced and retried exactly like a search."""
        self._pause()
        self.calls += 1
        try:
            return self.throttle.call(lambda: self._once(lambda: self.ytm.get_album(browse_id)),
                                      ingest.transient_search, "YouTube Music album")
        finally:
            self.last_end = self.clock()


# ---- one album, then the run ----

@dataclass
class CoverBackends:
    """Everything outside the process. Tests pass fakes."""
    find_match: Callable[[Todo], object | None]       # the shared matcher on the stored track; None = no match
    upload_cover: Callable[[dict, Path], str]         # ingest's upload_cover: {"cover_url": ...} -> Telegram file_id
    d1_write: Callable[[str, list], object]           # one parameterized statement -> D1 result
    ytm: object | None = None                         # paced client (search, get_album) of the album-level source


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
        d1_write=lambda sql, params: ingest.d1_batch([(sql, params)]),
        ytm=paced)


def changed_rows(result) -> int | None:
    """meta.changes of the D1 answer for one statement, None when the shape is unexpected."""
    try:
        n = result[0]["meta"]["changes"]
    except (KeyError, IndexError, TypeError):
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def _store_cover(todo: Todo, url: str, backends: CoverBackends) -> str:
    """Fetch + upload the cover and write it with the idempotent UPDATE: "covered", or "already" when a stored cover won."""
    with tempfile.TemporaryDirectory() as tmp:
        file_id = backends.upload_cover({"cover_url": url, ingest.COVER_FROM_MATCH: True}, Path(tmp))
    if not isinstance(file_id, str) or not file_id:
        raise ingest.IngestError("no file id")
    changes = changed_rows(backends.d1_write(UPDATE_SQL, [file_id, todo.album_id]))
    return "already" if changes == 0 else "covered"


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
        return _store_cover(todo, url, backends)
    except ingest.YouTubeSignInRequired:
        raise
    except Exception as e:  # noqa: BLE001 - the sign-in stop was re-raised above; any other error is this album's failure
        ingest.log(f"WARN covers: attempt failed ({type(e).__name__})")
        return "failed"


def cooldown_result(outcome: str) -> str | None:
    if outcome == "no_match":
        return "no_match"
    if outcome in ("album_mismatch", "artist_mismatch", "no_cover_in_match"):
        return "mismatch"
    if outcome == "failed":
        return "failed"
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
                 today: Callable[[], datetime.date], retry_days: int,
                 failed_days: int = DEFAULT_FAILED_DAYS,
                 attempt_fn: Callable[[Todo, CoverBackends], str] | None = None) -> CoverRun:
    """Works `todo` in order until a cap, the time limit, a sign-in challenge or too many failures in a row. --max-new
    counts albums ATTEMPTED (every attempt costs search calls); cooldown albums are skipped and not counted."""
    run = CoverRun()
    consecutive = 0
    attempt_fn = attempt_fn or attempt
    for i, t in enumerate(todo):
        if state.cooled(t.album_id, today(), retry_days, failed_days):
            run.cooldown += 1
            continue
        reason = limits.exhausted()
        if reason:
            run.stop_reason = reason
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days) for x in todo[i:])
            break
        limits.attempted += 1
        try:
            outcome = attempt_fn(t, backends)
        except ingest.YouTubeSignInRequired:
            run.interrupted, run.stop_reason = True, "sign-in"
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days) for x in todo[i:])
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
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days) for x in todo[i + 1:])
            break
    return run


# ---- the dry count ----

def count_lines(query: Callable[[str, list | None], list], state: CoverState, today: datetime.date,
                retry_days: int, failed_days: int = DEFAULT_FAILED_DAYS) -> list[str]:
    """`covers --count`: D1 reads only (the same album list as the run, one count of artists). No YouTube, no Telegram,
    no write, no file. Lines hold counts only. The artist half reports `not counted` while the column does not exist."""
    todo, skipped = read_todo(query)
    cooled = sum(state.cooled(t.album_id, today, retry_days, failed_days) for t in todo)
    lines = [f"covers: albums without a cover: {len(todo) + skipped['unknown'] + skipped['bad']} "
             f"(to try: {len(todo) - cooled}, in cooldown: {cooled}, unknown album: {skipped['unknown']}, "
             f"unreadable rows: {skipped['bad']}; retry cooldown {retry_days} days, failed {failed_days} days)"]
    # The column is checked first with a query that cannot fail on a missing column; any D1 error in this optional half
    # (D1 answers a SQL error with HTTP 400, which d1_batch reports without the SQL message) leaves the albums line printed.
    try:
        if not query(ARTISTS_COLUMN_SQL, None):
            lines.append("covers: artists without an image: not counted yet (the artists.image_file_id column does not exist)")
        else:
            n = query(ARTISTS_COUNT_SQL, None)[0]["n"]
            if isinstance(n, bool) or not isinstance(n, int):
                raise ingest.IngestError("unexpected artist count")
            lines.append(f"covers: artists without an image: {n} (in cooldown: 0)")
    except (ingest.IngestError, IndexError, KeyError, TypeError) as e:
        ingest.log(f"WARN covers: the artist count failed ({type(e).__name__})")
        lines.append("covers: artists without an image: not counted yet (the artist query failed)")
    return lines


# ---- the second source, the dry run and the plan file (#352) ----

PLAN_FILE = "cover-plan.json"
PLAN_VERSION = 1
PLAN_MAX_ENTRIES = 5000
RULES = ("track", "normalised", "album_search")  # which rule accepts: the strict guard, the narrow normalisation, album-level
ALBUM_SEARCH_LIMIT = 5   # hits looked at per album
MAX_ALBUM_FETCHES = 3    # get_album calls per album at most
BROWSE_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
# the reason words of the album-level source, from the earliest to the latest guard it reached
SECOND_REASONS = ("no_hits", "album_mismatch", "artist_mismatch", "pseudo_artist", "no_browse_id", "tracklist",
                  "no_thumbnail", "unavailable")


class PlanError(Exception):
    """The plan file must not be used. The message is a reason without any file content."""


@dataclass
class AlbumResult:
    url: str | None
    browse_id: str | None
    reason: str  # "ok" or one of SECOND_REASONS


def tracklist_has(page, title: str, duration_s: int | None = None) -> bool:
    """True when the album page lists a track that is the stored title and the same variant markers (a live or remix
    track is another recording). With both durations known (stored track and the page's `duration_seconds`) the matcher's
    bars apply: title similarity >= TITLE_MIN_SIMILARITY AND the durations within +-DURATION_TOLERANCE_S. When either
    duration is unknown (ytmusicapi may omit it) there is nothing to back the title, so the stricter title-only bar
    TITLE_ONLY_TITLE_MIN_SIMILARITY (0.93) applies instead: refusing every such page would block the source, accepting a
    0.86 title on its own would take numbered titles ('Track A' / 'Track B') for the same track."""
    tracks = page.get("tracks") if isinstance(page, dict) else None
    known = isinstance(duration_s, int) and not isinstance(duration_s, bool) and duration_s > 0
    for t in tracks if isinstance(tracks, list) else []:
        name = t.get("title") if isinstance(t, dict) else None
        if not isinstance(name, str) or not name:
            continue
        found = t.get("duration_seconds")
        both = known and isinstance(found, int) and not isinstance(found, bool) and found > 0
        if both and abs(found - duration_s) > match.DURATION_TOLERANCE_S:
            continue
        bar = match.TITLE_MIN_SIMILARITY if both else match.TITLE_ONLY_TITLE_MIN_SIMILARITY
        if max(match.similarity(title, name), match.relaxed_similarity(title, name)) >= bar \
                and match.title_parts(title)[1] == match.title_parts(name)[1]:
            return True
    return False


def album_level(todo: Todo, ytm) -> AlbumResult:
    """The second source: one search with filter `albums` for the stored artist + album, every hit through the guards
    (narrow album compare, artist check with the Cyrillic bar, pseudo-artist denylist), then the tracklist check
    (get_album must list the stored track). The first hit that passes all of it wins; its own thumbnail is the cover."""
    if match.normalize(todo.artist) in match.PSEUDO_ARTISTS:
        return AlbumResult(None, None, "pseudo_artist")
    hits = ytm.search(f"{todo.artist} {todo.album}", filter="albums", limit=ALBUM_SEARCH_LIMIT)
    reached = 0  # index into SECOND_REASONS of the furthest guard any hit reached
    fetched = 0
    for hit in hits[:ALBUM_SEARCH_LIMIT] if isinstance(hits, list) else []:
        if not isinstance(hit, dict) or not isinstance(hit.get("title"), str):
            continue
        if not match.albums_match(todo.album, hit["title"], narrow=True):
            reached = max(reached, 1)
            continue
        names = [a["name"] for a in hit.get("artists") or []
                 if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]]
        if not names or not match.artists_match(todo.artist, names):
            reached = max(reached, 2)
            continue
        if any(match.normalize(n) in match.PSEUDO_ARTISTS for n in names):
            reached = max(reached, 3)
            continue
        bid = hit.get("browseId")
        if not isinstance(bid, str) or not BROWSE_ID.fullmatch(bid):
            reached = max(reached, 4)
            continue
        if fetched >= MAX_ALBUM_FETCHES:
            break
        fetched += 1
        page = ytm.get_album(bid)
        if not tracklist_has(page, todo.title, todo.duration_s):
            reached = max(reached, 5)
            continue
        base = match.thumbnail_base(hit) or match.thumbnail_base(page)
        if base is None:
            reached = max(reached, 6)
            continue
        return AlbumResult(base + match.COVER_SIZE_SUFFIX, bid, "ok")
    return AlbumResult(None, None, SECOND_REASONS[reached])


@dataclass
class Decision:
    rule: str | None            # one of RULES, None = rejected
    url: str | None = None
    browse_id: str | None = None
    first: str = "ok"           # outcome word of the track-level source
    second: str | None = None   # reason word of the album-level source (None when it was not needed)


def classify(todo: Todo, backends: CoverBackends) -> Decision:
    """Which rule would accept this album. Reads YouTube Music only; stores nothing."""
    m = backends.find_match(todo)
    first = "no_match"
    if m is not None:
        url, why = match.cover_url_from_match(todo.album, todo.artist, m)
        if url:
            return Decision("track", url)
        first = _GUARD.get(why, ("no_cover_in_match", "mismatch"))[0]
        url, _ = match.cover_url_from_match(todo.album, todo.artist, m, narrow=True)
        if url:
            return Decision("normalised", url, first=first)
    if backends.ytm is None:
        return Decision(None, first=first, second="unavailable")
    r = album_level(todo, backends.ytm)
    if r.url:
        return Decision("album_search", r.url, r.browse_id, first, "ok")
    return Decision(None, first=first, second=r.reason)


@dataclass
class PlanRun:
    accepts: dict = field(default_factory=lambda: {k: 0 for k in RULES})
    first_rejects: dict = field(default_factory=dict)    # track-level outcome word -> albums the strict first source did not accept
    second_rejects: dict = field(default_factory=dict)   # album-level reason word -> albums rejected by every source
    entries: dict = field(default_factory=dict)          # album id -> {"rule", "browse_id", "cover_url"}
    failed: int = 0
    cooldown: int = 0
    not_attempted: int = 0
    stop_reason: str = "done"
    interrupted: bool = False


def run_plan(todo: list[Todo], backends: CoverBackends, state: CoverState, limits: "ingest.RunLimits",
             today: Callable[[], datetime.date], retry_days: int, failed_days: int = DEFAULT_FAILED_DAYS) -> PlanRun:
    """The dry run: classify the albums in order under the same caps, cooldown and stops as the real run. It touches
    neither D1 nor Telegram (the backends' write and upload are never called) and not the cooldown file."""
    run = PlanRun()
    consecutive = 0
    for i, t in enumerate(todo):
        if state.cooled(t.album_id, today(), retry_days, failed_days):
            run.cooldown += 1
            continue
        reason = limits.exhausted()
        if reason:
            run.stop_reason = reason
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days) for x in todo[i:])
            break
        limits.attempted += 1
        try:
            d = classify(t, backends)
        except ingest.YouTubeSignInRequired:
            run.interrupted, run.stop_reason = True, "sign-in"
            run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days) for x in todo[i:])
            break
        except Exception as e:  # noqa: BLE001 - one album's failure; the sign-in stop was handled above
            ingest.log(f"WARN covers: dry-run attempt failed ({type(e).__name__})")
            run.failed += 1
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                run.stop_reason = "failed"
                run.not_attempted = sum(not state.cooled(x.album_id, today(), retry_days, failed_days)
                                        for x in todo[i + 1:])
                break
            continue
        consecutive = 0
        if d.rule != "track":  # the strict first source did not accept this album (a later rule may have)
            run.first_rejects[d.first] = run.first_rejects.get(d.first, 0) + 1
        if d.rule:
            run.accepts[d.rule] += 1
            run.entries[t.album_id] = {"rule": d.rule, "browse_id": d.browse_id, "cover_url": d.url}
        else:
            run.second_rejects[d.second] = run.second_rejects.get(d.second, 0) + 1
        ingest.log(f"covers: dry-run album {limits.attempted}: {d.rule or 'rejected'}")
    return run


def plan_lines(run: PlanRun) -> list[str]:
    """The dry run's public output: counts and class words only."""
    def fmt(d: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"
    return [f"covers dry-run: would accept {sum(run.accepts.values())} ("
            + ", ".join(f"{k} {v}" for k, v in run.accepts.items()) + ")",
            f"covers dry-run: first source did not accept: {fmt(run.first_rejects)}",
            f"covers dry-run: rejected by every source (album-level reason): {fmt(run.second_rejects)}",
            f"covers dry-run: failed {run.failed}, cooldown {run.cooldown}, not attempted {run.not_attempted}, "
            f"stop reason {run.stop_reason}"]


def write_plan(path: Path, entries: dict, today: datetime.date) -> None:
    """The plan file: private (0600, directory 0700), ids, rule words and cover URLs only, never a name. `approved` is
    always written false: only the owner sets it, by hand, after reviewing the plan."""
    ingest.atomic_write_json(path, {"version": PLAN_VERSION, "approved": False, "created": today.isoformat(),
                                    "entries": entries})


@dataclass
class Plan:
    approved: bool
    entries: dict


PLAN_MAX_AGE_DAYS = 14


def load_plan(path: Path, today: datetime.date | None = None) -> Plan:
    """The plan for an apply run; PlanError for anything unusable. Strict: unknown fields, a malformed entry and a plan
    that anybody but the owner can write (group/world writable, or owned by another user) are refused, and so is a plan
    older than PLAN_MAX_AGE_DAYS (the library moves on; run a new dry run). `approved` must be the JSON value true."""
    try:
        st = path.stat()
    except OSError as e:
        raise PlanError(f"cannot be read ({type(e).__name__})") from None
    if st.st_mode & 0o022:
        raise PlanError("is group or world writable; chmod 600 it")
    if st.st_uid != os.geteuid():
        raise PlanError("is not owned by the current user")
    try:
        doc = _read_state_doc(path)
    except CoverStateError as e:
        raise PlanError(str(e)) from None
    if not isinstance(doc, dict) or set(doc) != {"version", "approved", "created", "entries"}:
        raise PlanError("missing or unknown fields")
    if isinstance(doc["version"], bool) or doc["version"] != PLAN_VERSION or not isinstance(doc["approved"], bool) \
            or not _iso_date(doc["created"]):
        raise PlanError("unknown version or malformed header")
    if today is not None and (today - datetime.date.fromisoformat(doc["created"])).days > PLAN_MAX_AGE_DAYS:
        raise PlanError(f"is older than {PLAN_MAX_AGE_DAYS} days; run a new dry run")
    entries = doc["entries"]
    if not isinstance(entries, dict) or len(entries) > PLAN_MAX_ENTRIES:
        raise PlanError("entries is not an object of a sane size")
    for key, e in entries.items():
        ok = (isinstance(key, str) and ALBUM_ID.fullmatch(key) and isinstance(e, dict)
              and set(e) == {"rule", "browse_id", "cover_url"} and e["rule"] in RULES
              and (e["browse_id"] is None or (isinstance(e["browse_id"], str) and BROWSE_ID.fullmatch(e["browse_id"])))
              and isinstance(e["cover_url"], str) and len(e["cover_url"]) <= 600 and e["cover_url"].startswith("https://")
              and e["cover_url"].endswith(match.COVER_SIZE_SUFFIX) and not re.search(r"\s", e["cover_url"]))
        if not ok:
            raise PlanError("an entry is malformed")
    return Plan(doc["approved"], {k: dict(v) for k, v in entries.items()})


def apply_planned(plan: Plan) -> Callable[[Todo, CoverBackends], str]:
    """The attempt of an apply run: store exactly the planned cover of the album. No YouTube call, no new decision."""
    def apply(todo: Todo, backends: CoverBackends) -> str:
        try:
            return _store_cover(todo, plan.entries[todo.album_id]["cover_url"], backends)
        except Exception as e:  # noqa: BLE001 - one album's failure
            ingest.log(f"WARN covers: apply failed ({type(e).__name__}){ingest.safe_failure_context(e)}")
            return "failed"
    return apply

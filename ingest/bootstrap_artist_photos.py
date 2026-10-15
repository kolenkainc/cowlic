"""The logic of `bootstrap.py covers --artists` (#253): backfill the photo of artists stored without one. Design:
docs/cover-art.md sections 7 and 8, runbook: docs/bootstrap.md section 11.

For each artist with `image_file_id IS NULL` (one stored track per artist: the smallest track id) it runs the SAME matcher
as ingest (match.find_match), applies the SAME artist guard as ingest (match.artist_id_from_match: the matched FIRST
artist must fit the stored name, pseudo-artists are refused, the Cyrillic bar applies), asks YouTube Music for the
artist page once (the deps.artist_photo_url of ingest.real_deps: ytmusicapi get_artist, thumbnail suffix
match.ARTIST_PHOTO_SUFFIX), fetches the image through the hardened route (deps.upload_artist_image: https only, exact host
allowlist, no redirects, image/jpeg + FF D8, 10 MB cap, 60 s deadline), uploads it as a Telegram photo and writes

    UPDATE artists SET image_file_id = ? WHERE id = ? AND image_file_id IS NULL

(parameterized, idempotent: a photo another process stored meanwhile is never replaced). No audio is downloaded, and the
artist guard gates only this photo. Nothing here is a second matcher: only the glue and the cooldown live here.

No lock, no 21:10 guard and no command line here: bootstrap.py owns them (it imports this module, never the other way
round). The only local file is `artist-photo-backfill.json` (artist id -> result word and date; ids are hashes, no names).
While the column artists.image_file_id does not exist (migration 0003 not applied) the work list is None: a clean skip.

Never bypassed: a YouTube sign-in challenge ends the run (YouTubeSignInRequired, exit 3 in bootstrap.py): no cookies,
proxies, PO-token tricks or client rotation (#62). Logs carry counts and class words only, never a name or a title.
"""
import datetime
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import bootstrap_covers as bc
import ingest
import match

STATE_FILE = "artist-photo-backfill.json"
UPDATE_SQL = "UPDATE artists SET image_file_id = ? WHERE id = ? AND image_file_id IS NULL"

# One stored track per photo-less artist (the smallest track id over all the artist's albums) and that track's album.
# Read-only and parameterless.
ARTISTS_SQL = ("SELECT ar.id AS artist_id, ar.name AS artist, al.name AS album, t.title AS title, t.duration_s AS duration_s "
               "FROM artists ar "
               "JOIN tracks t ON t.id = (SELECT MIN(t2.id) FROM tracks t2 JOIN albums a2 ON a2.id = t2.album_id "
               "WHERE a2.artist_id = ar.id) "
               "JOIN albums al ON al.id = t.album_id "
               "WHERE ar.image_file_id IS NULL ORDER BY ar.id")

OUTCOMES = ("photographed", "already", "no_match", "artist_mismatch", "no_photo", "failed")
# the guard's reason words (match.artist_id_from_match) -> the outcome word; every guard refusal rests as "mismatch"
_GUARD = {"artist_mismatch": "artist_mismatch", "pseudo_artist": "artist_mismatch", "no_artist_id": "no_photo"}


class ArtistPhotoState(bc.CoverState):
    """artist-photo-backfill.json: {"version": 1, "artists": {<artist id>: {"result": ..., "on": "YYYY-MM-DD"}}}, same
    strict rules and atomic writes as the covers cooldown file (the base class)."""
    FILE_KEY = "artists"


@dataclass
class ArtistTodo:
    artist_id: str
    artist: str
    album: str | None   # None for the unknown album (the matcher then gets no album, as ingest does)
    title: str
    duration_s: int | None


def column_present(query: Callable[[str, list | None], list]) -> bool:
    """The gate: a query that cannot fail on a missing column (pragma_table_info), as ingest.load_artist_images."""
    return bool(query(bc.ARTISTS_COLUMN_SQL, None))


def read_todo(query: Callable[[str, list | None], list]) -> tuple[list[ArtistTodo] | None, dict]:
    """(artists to work in id order, {"bad": n}); None as the list when the column does not exist. Rows are validated."""
    if not column_present(query):
        return None, {"bad": 0}
    out: list[ArtistTodo] = []
    bad = 0
    for r in query(ARTISTS_SQL, None):
        d = r if isinstance(r, dict) else {}
        aid, name, title, album = d.get("artist_id"), d.get("artist"), d.get("title"), d.get("album")
        if not (isinstance(aid, str) and bc.ALBUM_ID.fullmatch(aid) and all(isinstance(x, str) and x.strip() for x in (name, title))
                and isinstance(album, str)):
            bad += 1
            continue
        dur = d.get("duration_s")
        out.append(ArtistTodo(aid, name, None if album.strip().casefold() == ingest.UNKNOWN_ALBUM.casefold() else album, title,
                              dur if isinstance(dur, int) and not isinstance(dur, bool) and dur > 0 else None))
    return out, {"bad": bad}


@dataclass
class ArtistBackends:
    """Everything outside the process. Tests pass fakes."""
    find_match: Callable[[ArtistTodo], object | None]   # the shared matcher on the stored track; None = no match
    artist_photo_url: Callable[[str], str | None]       # ingest's deps.artist_photo_url: browse id -> square-crop URL
    upload_image: Callable[[str, Path], str]            # ingest's deps.upload_artist_image: URL, dir -> Telegram file_id
    d1_write: Callable[[str, list], object]             # one parameterized statement -> D1 result


def real_backends() -> ArtistBackends:
    """The real network side: the shared matcher through the paced client of covers, the artist page and image upload of
    ingest.real_deps (hardened fetch; its get_artist is paced and retried by the Throttle), D1 writes via ingest.d1_batch."""
    from ytmusicapi import YTMusic
    ytm = YTMusic()
    paced = bc.PacedSearch(ytm, ingest.Throttle(0.0, 0.0, ingest.DEFAULT_ATTEMPTS))
    deps = ingest.real_deps(ytm, ingest.Throttle(0.0, 0.0, ingest.DEFAULT_ATTEMPTS))
    return ArtistBackends(
        find_match=lambda t: match.find_match(paced, t.title, t.artist, t.album, t.duration_s),
        artist_photo_url=deps.artist_photo_url,
        upload_image=deps.upload_artist_image,
        d1_write=lambda sql, params: ingest.d1_batch([(sql, params)]))


def attempt(todo: ArtistTodo, backends: ArtistBackends) -> str:
    """One artist: the outcome word. Raises YouTubeSignInRequired (never caught here); every other expected failure is the
    word `failed` (the exception type is logged, never its text)."""
    try:
        m = backends.find_match(todo)
        if m is None:
            return "no_match"
        aid, why = match.artist_id_from_match(todo.artist, m)  # the guard of ingest, unchanged
        if aid is None:
            return _GUARD.get(why, "artist_mismatch")
        url = backends.artist_photo_url(aid)
        if not url:
            return "no_photo"
        with tempfile.TemporaryDirectory() as tmp:
            file_id = backends.upload_image(url, Path(tmp))
        if not isinstance(file_id, str) or not file_id:
            raise ingest.IngestError("no file id")
        changes = bc.changed_rows(backends.d1_write(UPDATE_SQL, [file_id, todo.artist_id]))
        return "already" if changes == 0 else "photographed"
    except ingest.YouTubeSignInRequired:
        raise
    except Exception as e:  # noqa: BLE001 - the sign-in stop was re-raised above; any other error is this artist's failure
        ingest.log(f"WARN artist photos: attempt failed ({type(e).__name__})")
        return "failed"


def cooldown_result(outcome: str) -> str | None:
    return {"no_match": "no_match", "artist_mismatch": "mismatch", "no_photo": "mismatch", "failed": "failed"}.get(outcome)


@dataclass
class ArtistRun:
    counts: dict = field(default_factory=lambda: {k: 0 for k in OUTCOMES})
    cooldown: int = 0
    not_attempted: int = 0
    stop_reason: str = "done"
    interrupted: bool = False
    unsaved: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.counts["failed"] or self.unsaved)


def run_backfill(todo: list[ArtistTodo], backends: ArtistBackends, state: ArtistPhotoState, limits: "ingest.RunLimits",
                 today: Callable[[], datetime.date], retry_days: int,
                 failed_days: int = bc.DEFAULT_FAILED_DAYS) -> ArtistRun:
    """Works `todo` in order until a cap, the time limit, a sign-in challenge or too many failures in a row. --max-new
    counts artists ATTEMPTED; cooldown artists are skipped and not counted."""
    run = ArtistRun()
    consecutive = 0

    def left(items):
        return sum(not state.cooled(x.artist_id, today(), retry_days, failed_days) for x in items)

    for i, t in enumerate(todo):
        if state.cooled(t.artist_id, today(), retry_days, failed_days):
            run.cooldown += 1
            continue
        reason = limits.exhausted()
        if reason:
            run.stop_reason, run.not_attempted = reason, left(todo[i:])
            break
        limits.attempted += 1
        try:
            outcome = attempt(t, backends)
        except ingest.YouTubeSignInRequired:
            run.interrupted, run.stop_reason, run.not_attempted = True, "sign-in", left(todo[i:])
            break
        run.counts[outcome] += 1
        ingest.log(f"artist photos: artist {limits.attempted}: {outcome}")
        consecutive = consecutive + 1 if outcome == "failed" else 0
        changed = False
        cooled_as = cooldown_result(outcome)
        if cooled_as:
            state.record(t.artist_id, cooled_as, today())
            changed = True
        elif outcome in ("photographed", "already"):
            changed = state.forget(t.artist_id)
        if changed:
            try:
                state.save()
            except OSError as e:
                run.unsaved = True
                ingest.log(f"WARN artist photos: could not write {STATE_FILE} ({type(e).__name__}); the next artist retries")
            else:
                run.unsaved = False
        if consecutive >= bc.MAX_CONSECUTIVE_FAILURES:
            run.stop_reason, run.not_attempted = "failed", left(todo[i + 1:])
            break
    return run


def count_lines(query: Callable[[str, list | None], list], state: ArtistPhotoState, today: datetime.date,
                retry_days: int, failed_days: int = bc.DEFAULT_FAILED_DAYS) -> list[str]:
    """`covers --artists --count`: D1 reads only. No YouTube, no Telegram, no write, no file. Counts only."""
    todo, skipped = read_todo(query)
    if todo is None:
        return ["covers: artists without an image: not counted yet (the artists.image_file_id column does not exist)"]
    cooled = sum(state.cooled(t.artist_id, today, retry_days, failed_days) for t in todo)
    return [f"covers: artists without an image: {len(todo) + skipped['bad']} (to try: {len(todo) - cooled}, in cooldown: "
            f"{cooled}, unreadable rows: {skipped['bad']}; retry cooldown {retry_days} days, failed {failed_days} days)"]

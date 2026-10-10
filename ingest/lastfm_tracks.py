"""Enumerate the tracks of one Last.fm artist as ingest records (read-only, stdlib only, no I/O of its own).

artist.getTopAlbums (paged, limit 50, popularity order) -> album.getInfo per album (by mbid when present, else
artist + album, autocorrect on) -> one record per track, in the shape of `artist_tracks.song_record`. The client is
`lastfm.LastfmClient` (pacing >= 1 s, retries, key handling, redaction); nothing here ever sees the key.

Record: song_id, name, artists, album_artist, album_name, year (None), track_number, disc_number (1), duration (whole
seconds, or None when Last.fm gives 0, nothing or garbage: never invented, never 0), url (None), cover_url (None:
Last.fm artwork must not be used, ToS cl. 5.1.8 [R]; the pipeline then stores the album without a cover), plus
album_type / release_date / explicit (None) for ingest/dedupe.py. A consumer must cope with duration None.

Synthetic id (a contract with the SONG_ID check in ingest.py, which accepts exactly `lfm:[0-9a-f]{16}`):
    song_id = "lfm:" + sha256("<normalised artist>\\x1f<normalised title>").hexdigest()[:16]
where normalised = match.normalize(...) (NFKC, lower case, punctuation collapsed to single spaces), the artist is the
artist asked for (the one passed in, not the per-track artist), and the title is the track title as Last.fm gives it
(not tag-stripped: live / remix / acoustic stay distinct recordings). The album is NOT part of the id, so the same
artist + title on two albums is one track; the first album in Last.fm popularity order (then tracklist order) is kept.

Counts only are logged, never titles or names. Bounds per artist: MAX_ALBUMS albums and CALL_BUDGET HTTP requests
(retries included); an artist over the budget raises ArtistBudgetExceeded, never runs on.
"""
import hashlib
import sys
from dataclasses import dataclass
from typing import Callable

import lastfm
from lastfm import LastfmError, _MBID, _as_list, clean_text
from match import normalize

PAGE_LIMIT = 50  # albums per getTopAlbums page
MAX_ALBUMS = 100  # albums read per artist (the most popular ones)
CALL_BUDGET = 150  # HTTP requests per artist, retries included
MAX_DURATION_S = 36000  # a longer "track" is garbage: treated as unknown
MAX_TRACK_NO = 999
ID_PREFIX = "lfm:"

_ERR_NOT_FOUND = 6  # Last.fm: artist / album not found

# status of an enumeration
OK, UNKNOWN_ARTIST, NO_ALBUMS, UNUSABLE_ARTIST = "ok", "unknown_artist", "no_albums", "unusable_artist"


class ArtistBudgetExceeded(LastfmError):
    """The per-artist budget of HTTP requests is used up; the artist is abandoned, never run on."""


@dataclass
class ArtistTracks:
    records: list  # list[dict], deduplicated, album popularity order then tracklist order
    status: str  # OK, UNKNOWN_ARTIST, NO_ALBUMS or UNUSABLE_ARTIST
    albums: int = 0  # albums whose tracklist was read
    albums_not_found: int = 0  # albums Last.fm could not give (error 6)
    albums_dropped: int = 0  # album entries without a usable name or mbid, or listed twice
    albums_truncated: bool = False  # more albums existed than MAX_ALBUMS
    tracks: int = 0  # track entries seen
    dropped: int = 0  # entries without a usable title or artist
    duplicates: int = 0  # collapsed into an earlier record
    no_duration: int = 0  # records kept with duration None
    calls: int = 0  # HTTP requests used for this artist


def summary(res: ArtistTracks) -> str:
    """One log line, counts only (no names, titles or ids)."""
    return (f"lastfm artist: status {res.status}, {res.albums} albums read ({res.albums_not_found} not found, "
            f"{res.albums_dropped} dropped{', truncated' if res.albums_truncated else ''}), {res.tracks} tracks seen, "
            f"{len(res.records)} kept, {res.duplicates} duplicates, {res.dropped} dropped, "
            f"{res.no_duration} without duration, {res.calls} calls")


# ---- id ----

def song_id(artist: str, title: str) -> str | None:
    """The synthetic id of a track, None when the artist or the title has nothing left after normalisation."""
    a, t = normalize(artist), normalize(title)
    if not a or not t:
        return None
    return ID_PREFIX + hashlib.sha256(f"{a}\x1f{t}".encode("utf-8")).hexdigest()[:16]


# ---- parsing (allowlist; entries that do not fit are dropped and counted, never rewritten) ----

def _text(value) -> str | None:
    """Control characters stripped, whitespace collapsed; None when empty or longer than lastfm.NAME_MAX."""
    if not isinstance(value, str):
        return None
    text = clean_text(value, lastfm.NAME_MAX + 1)
    return text if text and len(text) <= lastfm.NAME_MAX else None


def _mbid(value) -> str | None:
    return value.lower() if isinstance(value, str) and _MBID.fullmatch(value) else None


def _int(value, lo: int, hi: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        n = int(value)
    else:
        return None
    return n if lo <= n <= hi else None


def parse_albums_page(body: dict) -> tuple[list, int]:
    """(raw album entries, total pages) of an artist.getTopAlbums answer."""
    root = body.get("topalbums")
    if not isinstance(root, dict):
        raise LastfmError("malformed Last.fm response: `topalbums` is missing")
    attr = root.get("@attr")
    pages = _int(attr.get("totalPages"), 0, 10**6) if isinstance(attr, dict) else None
    return _as_list(root.get("album"), "album"), 1 if pages is None else pages


def parse_album_tracks(body: dict) -> tuple[str | None, list]:
    """(album name or None, raw track entries) of an album.getInfo answer."""
    root = body.get("album")
    if not isinstance(root, dict):
        raise LastfmError("malformed Last.fm response: `album` is missing")
    tracks = root.get("tracks")
    if tracks in (None, ""):
        return _text(root.get("name")), []
    if not isinstance(tracks, dict):
        raise LastfmError("malformed Last.fm response: `tracks` has an unexpected type")
    return _text(root.get("name")), _as_list(tracks.get("track"), "track")


def _track_artist(entry: dict) -> str | None:
    a = entry.get("artist")
    return _text(a.get("name")) if isinstance(a, dict) else _text(a)


def make_record(entry, artist: str, album_name: str | None) -> dict | None:
    """The ingest record of one track entry, None when it has no usable title or the id cannot be built."""
    if not isinstance(entry, dict):
        return None
    title = _text(entry.get("name"))
    sid = song_id(artist, title) if title else None
    if sid is None:
        return None
    other = _track_artist(entry)
    artists = [artist] if not other or normalize(other) in ("", normalize(artist)) else [artist, other]
    attr = entry.get("@attr")
    rank = _int(attr.get("rank"), 1, MAX_TRACK_NO) if isinstance(attr, dict) else None
    return {"song_id": sid, "name": title, "artists": artists, "album_artist": artist, "album_name": album_name,
            "year": None, "track_number": rank, "disc_number": 1,
            "duration": _int(entry.get("duration"), 1, MAX_DURATION_S),  # 0 / missing / garbage -> None
            "url": None, "cover_url": None, "album_type": None, "release_date": None, "explicit": None}


# ---- enumeration ----

def artist_tracks(client, artist: str, mbid: str | None = None, max_albums: int = MAX_ALBUMS,
                  call_budget: int = CALL_BUDGET,
                  out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> ArtistTracks:
    """Records for one Last.fm artist (`artist` as top_artists gives it, `mbid` optional). An unknown artist or one
    without albums gives an empty result with a status, not an exception. Raises ArtistBudgetExceeded when more than
    `call_budget` requests would be needed, LastfmError on any other failure (bad key, malformed answer, ...)."""
    name = _text(artist)
    if name is None:
        raise LastfmError(f"artist name must be 1-{lastfm.NAME_MAX} characters")
    if mbid is not None and _mbid(mbid) is None:
        raise LastfmError("artist mbid is malformed")
    for label, v in (("max_albums", max_albums), ("call_budget", call_budget)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise LastfmError(f"{label} must be a positive integer")
    res = ArtistTracks(records=[], status=OK)
    if not normalize(name):
        res.status = UNUSABLE_ARTIST
        out(summary(res))
        return res
    start = client.calls
    try:
        with client.call_budget(call_budget):
            _enumerate(client.call, name, mbid, max_albums, res)
    except lastfm.CallLimitReached:
        raise ArtistBudgetExceeded(f"Last.fm call limit reached (budget {call_budget} per artist); stopping this artist") from None
    res.calls = client.calls - start
    out(summary(res))
    return res


def _enumerate(call, name: str, mbid: str | None, max_albums: int, res: ArtistTracks) -> None:
    try:
        albums = _list_albums(call, name, mbid, max_albums, res)
    except LastfmError as e:
        if e.code != _ERR_NOT_FOUND:
            raise
        albums = None
    if albums is None:
        res.status = UNKNOWN_ARTIST
    elif not albums:
        res.status = NO_ALBUMS
    seen: set[str] = set()
    for alb in albums or []:
        params = {"mbid": alb["mbid"]} if alb["mbid"] else {"artist": name, "album": alb["name"], "autocorrect": "1"}
        try:
            body = call("album.getInfo", **params)
        except LastfmError as e:
            if e.code != _ERR_NOT_FOUND:
                raise
            res.albums_not_found += 1
            continue
        album_name, entries = parse_album_tracks(body)
        res.albums += 1
        for entry in entries:
            res.tracks += 1
            rec = make_record(entry, name, album_name or alb["name"])
            if rec is None:
                res.dropped += 1
            elif rec["song_id"] in seen:
                res.duplicates += 1
            else:
                seen.add(rec["song_id"])
                res.records.append(rec)
                res.no_duration += rec["duration"] is None


def _list_albums(call, name: str, mbid: str | None, max_albums: int, res: ArtistTracks) -> list:
    """Up to max_albums distinct albums [{name, mbid}] in popularity order; counts go to `res`."""
    albums: list[dict] = []
    keys: set[str] = set()
    page, total = 1, 1
    while page <= total:
        params = {"mbid": mbid} if mbid else {"artist": name, "autocorrect": "1"}
        entries, total = parse_albums_page(call("artist.getTopAlbums", limit=str(PAGE_LIMIT), page=str(page), **params))
        if not entries:
            break
        for e in entries:
            if len(albums) >= max_albums:
                res.albums_truncated = True
                return albums
            alb_name = _text(e.get("name")) if isinstance(e, dict) else None
            alb_mbid = _mbid(e.get("mbid")) if isinstance(e, dict) else None
            key = f"mbid:{alb_mbid}" if alb_mbid else f"name:{normalize(alb_name)}" if alb_name else ""
            if not key or key in keys or (not alb_mbid and not normalize(alb_name)):
                res.albums_dropped += 1
                continue
            keys.add(key)
            albums.append({"name": alb_name, "mbid": alb_mbid})
        if len(albums) >= max_albums and page < total:  # more pages exist: no call just to look at them
            res.albums_truncated = True
            return albums
        page += 1
    return albums

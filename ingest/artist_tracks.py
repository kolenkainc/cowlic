"""Enumerate the tracks of one Spotify artist and return song records the per-track pipeline takes.

Usage (prints the number of tracks and API calls; downloads nothing):
  python ingest/artist_tracks.py <artist id or https://open.spotify.com/artist/<id> URL>
         [--groups album,single] [--market US] [--include-other-album-artists]

Flow: GET /artists/{id}/albums (include_groups, limit=10, fixed market, follow `next`), then one
GET /albums/{id}/tracks (limit=50) per album. The batch endpoints are gone, so there is no cheaper way, and ISRC
is not fetched (it needs one call per track).

Auth: Client Credentials with the SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET the ingest already uses. The catalog
endpoints are public data, so no user login (and no refresh token) is needed, which keeps this usable from any
environment that can already run the ingest. Tokens and the secret never reach stdout or any error message.

Albums whose album artist is not the requested artist (compilations by "Various Artists", appears_on releases)
are skipped by default. With --include-other-album-artists they are fetched but only the tracks that list the
artist are kept.

Spotify docs relied on (read 2026-10-10):
  https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums
    include_groups: album, single, appears_on, compilation (all when omitted); limit default 5, max 10; offset;
    market (needed: with no user token and no market the content is "unavailable"); paging object with `next`.
  https://developer.spotify.com/documentation/web-api/reference/get-an-albums-tracks
    market, limit default 20, max 50, offset; items have id, name, artists, duration_ms, track_number,
    disc_number, is_local; no ISRC.
  https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow
  https://developer.spotify.com/documentation/web-api/concepts/rate-limits  (429 with Retry-After)
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import tg_check
from ingest import redact

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_MARKET = "US"
DEFAULT_GROUPS = ("album", "single")
ALL_GROUPS = ("album", "single", "compilation", "appears_on")
ALBUMS_LIMIT = 10
TRACKS_LIMIT = 50
HTTP_TIMEOUT = 10.0
MIN_INTERVAL = 0.2  # seconds between two API calls
MAX_RETRIES = 3  # retries per request after a 429 / 5xx / network error
MAX_RETRY_AFTER = 120  # seconds; a longer Retry-After aborts instead of sleeping
MAX_CALLS = 2000  # hard budget of API calls per enumeration (huge artists stop with an error, never run on)
MAX_ALBUMS = 1000

_ID = re.compile(r"[A-Za-z0-9]{22}")
_ARTIST_URL = re.compile(r"https://open\.spotify\.com/artist/([A-Za-z0-9]{22})(?:\?[A-Za-z0-9=&_.%-]{1,200})?")
_MARKET = re.compile(r"[A-Z]{2}")
_DIGITS = re.compile(r"[0-9]{1,6}")

# (url, headers, timeout) -> (HTTP status, lower-cased response headers, parsed JSON object or {}). Injected in tests.
Fetch = Callable[[str, dict, float], tuple[int, dict, dict]]


class ArtistTracksError(Exception):
    """Enumeration failure. The message is redacted and never holds a token, secret or response body."""


# ---- validation (allowlist; rejects, never rewrites) ----

def parse_artist(value: str) -> str:
    """A bare 22-char artist id or an https://open.spotify.com/artist/<id>[?si=...] URL -> the id."""
    if _ID.fullmatch(value):
        return value
    m = _ARTIST_URL.fullmatch(value)
    if not m:
        raise ArtistTracksError(f"not a Spotify artist id or URL (https://open.spotify.com/artist/<22 chars>): {value[:80]!r}")
    return m[1]


def parse_groups(value: str | tuple | list) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else list(value)
    if not items or any(g not in ALL_GROUPS for g in items):
        raise ArtistTracksError(f"include_groups must be a comma-separated subset of {','.join(ALL_GROUPS)}")
    return tuple(dict.fromkeys(items))


def parse_market(value: str) -> str:
    if not _MARKET.fullmatch(value):
        raise ArtistTracksError("market must be an upper-case ISO 3166-1 alpha-2 code such as US")
    return value


# ---- HTTP ----

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never follow: Authorization must not leave the API host
        return None  # the 30x then surfaces as an HTTPError and is reported as a failed request


_OPEN = urllib.request.build_opener(_NoRedirect).open


def default_fetch(url: str, headers: dict, timeout: float) -> tuple[int, dict, dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPEN(req, timeout=timeout) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    except (urllib.error.URLError, OSError) as e:  # includes timeouts; the class name only
        raise ArtistTracksError(f"network error talking to Spotify ({type(e).__name__})") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return status, {k.lower(): v for k, v in hdrs.items()}, body if isinstance(body, dict) else {}


def client_credentials_token(client_id: str, client_secret: str, timeout: float = HTTP_TIMEOUT,
                             url: str = TOKEN_URL) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url, data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _OPEN(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise ArtistTracksError(f"network error getting a Spotify token ({type(e).__name__})") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    tok = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(tok, str) or not tok:
        raise ArtistTracksError(f"Spotify token endpoint error: HTTP {status}")
    return tok


class Catalog:
    """Throttled, bounded GET client for the Spotify catalog. `token` is a zero-argument callable returning an
    access token (called once, and again after a 401). Counts every request in `calls`."""

    def __init__(self, token: Callable[[], str], secret_values: list[str], fetch: Fetch = default_fetch,
                 sleep: Callable[[float], None] = time.sleep, min_interval: float = MIN_INTERVAL,
                 max_calls: int = MAX_CALLS, out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)):
        self._token_fn, self._token = token, None
        self.secrets = secret_values
        self._fetch, self._sleep, self._interval, self._max_calls, self._out = fetch, sleep, min_interval, max_calls, out
        self.calls = 0
        self._last = 0.0

    def _err(self, msg: str) -> ArtistTracksError:
        return ArtistTracksError(redact(msg, self.secrets + ([self._token] if self._token else [])))

    def get(self, url: str) -> dict:
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            if self.calls >= self._max_calls:
                raise self._err(f"API call limit of {self._max_calls} reached; stopping")
            if self._token is None:
                self._token = self._token_fn()
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self.calls += 1
            try:
                status, headers, body = self._fetch(url, {"Authorization": f"Bearer {self._token}"}, HTTP_TIMEOUT)
            except ArtistTracksError as e:
                if attempt == MAX_RETRIES:
                    raise self._err(str(e)) from None
                self._sleep(2.0 ** attempt)
                continue
            finally:
                self._last = time.monotonic()
            if status == 200:
                return body
            if status == 401 and not refreshed:
                refreshed, self._token = True, None
                continue
            if status == 429 or status in (500, 502, 503, 504):
                if attempt == MAX_RETRIES:
                    raise self._err(f"Spotify kept answering HTTP {status} after {MAX_RETRIES} retries")
                ra = headers.get("retry-after", "")
                if status == 429 and _DIGITS.fullmatch(str(ra).strip()):
                    secs = int(str(ra).strip())
                    if secs > MAX_RETRY_AFTER:
                        raise self._err(f"Spotify asks to wait {secs} s (Retry-After); too long, stopping")
                else:
                    secs = 2.0 ** attempt
                self._out(f"HTTP {status}, waiting {secs} s before retry {attempt + 1}/{MAX_RETRIES}")
                self._sleep(secs)
                continue
            raise self._err(f"Spotify request failed: HTTP {status}")
        raise self._err("Spotify request failed after retries")  # unreachable, keeps the contract explicit


# ---- enumeration ----

def _page_items(body: dict) -> list[dict]:
    items = body.get("items")
    if not isinstance(items, list):
        raise ArtistTracksError("malformed Spotify response: `items` is missing or not a list")
    return [i for i in items if isinstance(i, dict)]


def _paged(cat: Catalog, first_url: str, path: str, limit_hint: str):
    """Yield items page by page, following `next` only while it stays on api.spotify.com under `path` and has
    not been seen before (a loop or a redirect to another host ends the enumeration with an error)."""
    url, seen = first_url, set()
    while url:
        if url in seen:
            raise ArtistTracksError("malformed Spotify response: `next` repeats a page")
        seen.add(url)
        body = cat.get(url)
        yield from _page_items(body)
        nxt = body.get("next")
        if nxt is None:
            return
        parts = urllib.parse.urlsplit(nxt) if isinstance(nxt, str) else None
        if not parts or (parts.scheme, parts.netloc, parts.path) != ("https", "api.spotify.com", f"/v1{path}"):
            raise ArtistTracksError(f"malformed Spotify response: unexpected `next` for {limit_hint}")
        url = nxt


def list_albums(cat: Catalog, artist: str, groups: tuple[str, ...], market: str, max_albums: int = MAX_ALBUMS) -> list[dict]:
    path = f"/artists/{artist}/albums"
    q = urllib.parse.urlencode({"include_groups": ",".join(groups), "limit": ALBUMS_LIMIT, "offset": 0, "market": market})
    albums, ids = [], set()
    for a in _paged(cat, f"{API}{path}?{q}", path, "artist albums"):
        aid = a.get("id")
        if not isinstance(aid, str) or not _ID.fullmatch(aid) or aid in ids:  # paging can repeat an album
            continue
        ids.add(aid)
        albums.append(a)
        if len(albums) > max_albums:
            raise ArtistTracksError(f"more than {max_albums} releases; stopping")
    return albums


def _year(release_date) -> int | None:
    m = re.match(r"([0-9]{4})(?:-|$)", release_date) if isinstance(release_date, str) else None
    return int(m[1]) if m else None


def _cover(album: dict) -> str | None:
    imgs = album.get("images")
    if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict):
        u = imgs[0].get("url")
        if isinstance(u, str) and u.startswith("https://"):
            return u
    return None


def _names(artists) -> list[str]:
    return [a["name"] for a in artists if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]] \
        if isinstance(artists, list) else []


def _has_artist(artists, artist_id: str) -> bool:
    return isinstance(artists, list) and any(isinstance(a, dict) and a.get("id") == artist_id for a in artists)


def song_record(track: dict, album: dict) -> dict | None:
    """The dict shape `spotdl save` gives and `ingest.process_track` takes (song_id, name, artists, album_artist,
    album_name, year, track_number, disc_number, duration in seconds, url, cover_url). None for a track that
    cannot be ingested (local file, bad id, no name, no duration)."""
    tid, name, ms = track.get("id"), track.get("name"), track.get("duration_ms")
    if (not isinstance(tid, str) or not _ID.fullmatch(tid) or track.get("is_local") is True
            or not isinstance(name, str) or not name
            or isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms <= 0):
        return None
    album_artists = _names(album.get("artists"))
    rec = {
        "song_id": tid, "name": name, "artists": _names(track.get("artists")),
        "album_artist": album_artists[0] if album_artists else None,
        "album_name": album.get("name") if isinstance(album.get("name"), str) else None,
        "year": _year(album.get("release_date")),
        "track_number": track.get("track_number") if isinstance(track.get("track_number"), int) else None,
        "disc_number": track.get("disc_number") if isinstance(track.get("disc_number"), int) else None,
        "duration": ms / 1000.0, "url": f"https://open.spotify.com/track/{tid}", "cover_url": _cover(album),
        # extra fields for cross-release dedupe (ingest/dedupe.py); the pipeline ignores them
        "album_type": album.get("album_type") if isinstance(album.get("album_type"), str) else None,
        "release_date": album.get("release_date") if isinstance(album.get("release_date"), str) else None,
        "explicit": track.get("explicit") if isinstance(track.get("explicit"), bool) else None}
    return rec


def artist_tracks(cat: Catalog, artist: str, groups=DEFAULT_GROUPS, market: str = DEFAULT_MARKET,
                  include_other_album_artists: bool = False, max_albums: int = MAX_ALBUMS,
                  out: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> list[dict]:
    """Song records for `artist` (id or URL), de-duplicated by song_id only (cross-release dedupe is #117)."""
    artist_id, groups, market = parse_artist(artist), parse_groups(groups), parse_market(market)
    albums = list_albums(cat, artist_id, groups, market, max_albums)
    out(f"{len(albums)} releases listed ({','.join(groups)}, market {market})")
    songs, seen, skipped_albums, dropped = [], set(), 0, 0
    for n, album in enumerate(albums, 1):
        own = _has_artist(album.get("artists"), artist_id)
        if not own and not include_other_album_artists:
            skipped_albums += 1
            continue
        path = f"/albums/{album['id']}/tracks"
        q = urllib.parse.urlencode({"limit": TRACKS_LIMIT, "offset": 0, "market": market})
        for t in _paged(cat, f"{API}{path}?{q}", path, "album tracks"):
            if not own and not _has_artist(t.get("artists"), artist_id):
                continue
            rec = song_record(t, album)
            if rec is None:
                dropped += 1
            elif rec["song_id"] not in seen:
                seen.add(rec["song_id"])
                songs.append(rec)
        out(f"album {n}/{len(albums)}: {len(songs)} tracks so far, {cat.calls} API calls")
    if skipped_albums:
        out(f"{skipped_albums} releases skipped (album artist is not the artist)")
    if dropped:
        out(f"{dropped} tracks skipped (local, or missing id / name / duration)")
    return songs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Count the tracks of a Spotify artist (no download).")
    ap.add_argument("artist", help="artist id or https://open.spotify.com/artist/<id> URL")
    ap.add_argument("--groups", default=",".join(DEFAULT_GROUPS), help="include_groups subset (default album,single)")
    ap.add_argument("--market", default=DEFAULT_MARKET)
    ap.add_argument("--include-other-album-artists", action="store_true")
    args = ap.parse_args(argv)
    secrets = []
    try:
        tg_check.load_env()
        cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not cid or not secret:
            raise ArtistTracksError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
        secrets = [cid, secret]
        cat = Catalog(lambda: client_credentials_token(cid, secret), secrets)
        songs = artist_tracks(cat, args.artist, args.groups, args.market, args.include_other_album_artists)
    except (ArtistTracksError, tg_check.EnvSyntaxError) as e:
        print(f"ERROR {redact(str(e), secrets)}", file=sys.stderr)
        return 1
    print(f"{len(songs)} tracks, {cat.calls} API calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())

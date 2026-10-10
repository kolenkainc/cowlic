"""Sync one public Spotify playlist into the library: match -> download -> Telegram -> D1.

Usage: python ingest/ingest.py PLAYLIST_URL [--dry-run]

Env (or ./.env): TG_BOT_TOKEN, TG_CHANNEL_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID. Secrets are never printed.
Tracks whose spotify_id is already in D1 are skipped, so re-running adds nothing.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tg_check
from match import find_match

MAX_BYTES = 20 * 1024 * 1024  # Telegram Bot API getFile limit
BITRATE = "256k"
REQUIRED_ENV = ["TG_BOT_TOKEN", "TG_CHANNEL_ID", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID"]


SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
COVER_MAX_BYTES = 10 * 1024 * 1024
UNKNOWN_ARTIST, UNKNOWN_ALBUM = "Unknown Artist", "Unknown Album"


class IngestError(Exception):
    """Per-track failure with a message that is safe to print (no tokens, no URLs with tokens)."""


# ---- pure logic (unit tested) ----

def primary_artist(song: dict) -> str:
    return song.get("album_artist") or (song.get("artists") or [None])[0] or UNKNOWN_ARTIST


def dedupe(songs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for s in songs:
        if s.get("song_id") not in seen:
            seen.add(s.get("song_id"))
            out.append(s)
    return out


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text

def stable_id(*parts: str) -> str:
    """Stable 16-hex id from normalized parts; identical input always gives the same id."""
    key = "\x1f".join(p.strip().casefold() for p in parts)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def artist_id(name: str) -> str:
    return stable_id("artist", name)


def album_id(artist: str, album: str) -> str:
    return stable_id("album", artist, album)


def track_id(spotify_id: str) -> str:
    return stable_id("track", spotify_id)


def too_big(size_bytes: int) -> bool:
    return size_bytes > MAX_BYTES


def upsert_statements(song: dict, size_bytes: int, tg_file_id: str, tg_message_id: int,
                      cover_file_id: str | None) -> list[tuple[str, list]]:
    """Parameterized SQL for one track. Existing cover_file_id is kept when no new cover is given."""
    artist, album = primary_artist(song), song.get("album_name") or UNKNOWN_ALBUM
    ar, al, tr = artist_id(artist), album_id(artist, album), track_id(song["song_id"])
    return [
        ("INSERT INTO artists (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
         [ar, artist]),
        ("INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES (?, ?, ?, ?, ?) "
         "ON CONFLICT(id) DO UPDATE SET name = excluded.name, year = excluded.year, "
         "cover_file_id = COALESCE(excluded.cover_file_id, albums.cover_file_id)",
         [al, ar, album, song.get("year") or None, cover_file_id]),
        ("INSERT INTO tracks (id, spotify_id, album_id, title, track_no, disc_no, duration_s, suffix, "
         "content_type, size_bytes, tg_file_id, tg_message_id) "
         "VALUES (?, ?, ?, ?, ?, ?, ?, 'mp3', 'audio/mpeg', ?, ?, ?) "
         "ON CONFLICT(id) DO UPDATE SET title = excluded.title, album_id = excluded.album_id, "
         "track_no = excluded.track_no, disc_no = excluded.disc_no, duration_s = excluded.duration_s, "
         "size_bytes = excluded.size_bytes, tg_file_id = excluded.tg_file_id, "
         "tg_message_id = excluded.tg_message_id",
         [tr, song["song_id"], al, song["name"], song.get("track_number"), song.get("disc_number"),
          int(song["duration"]), size_bytes, tg_file_id, tg_message_id]),
    ]


def largest_photo_file_id(photos: list[dict]) -> str:
    return max(photos, key=lambda p: p.get("file_size") or p["width"] * p["height"])["file_id"]


# ---- I/O ----

def d1_batch(statements: list[tuple[str, list]]) -> list[dict]:
    """Run statements in one D1 REST call ({"batch": [{"sql", "params"}, ...]})."""
    url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
           f"/d1/database/{os.environ['D1_DATABASE_ID']}/query")
    payload = {"batch": [{"sql": sql, "params": params} for sql, params in statements]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "content-type": "application/json", "authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise IngestError(f"D1 API error {e.code}") from None
    if not body.get("success"):
        raise IngestError(f"D1 query failed: {[e.get('message') for e in body.get('errors', [])]}")
    return body["result"]


def d1_query(sql: str, params: list | None = None) -> list[dict]:
    return d1_batch([(sql, params or [])])[0].get("results", [])


def tg_upload(method: str, field: str, path: Path, content_type: str, extra: dict | None = None) -> dict:
    boundary, parts = tg_check.multipart({"chat_id": os.environ["TG_CHANNEL_ID"], **(extra or {})}, field, path,
                                         content_type)
    req = urllib.request.Request(f"{tg_check.API}/bot{os.environ['TG_BOT_TOKEN']}/{method}", data=b"".join(parts),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    try:
        return tg_check._send(req)["result"]
    except tg_check.TelegramError as e:
        raise IngestError(str(e)) from None


def tg_delete(message_id: int) -> None:
    req = urllib.request.Request(f"{tg_check.API}/bot{os.environ['TG_BOT_TOKEN']}/deleteMessage",
                                 data=json.dumps({"chat_id": os.environ["TG_CHANNEL_ID"],
                                                  "message_id": message_id}).encode(),
                                 headers={"content-type": "application/json"})
    tg_check._send(req)


def spotdl_run(args: list[str], cwd: Path) -> None:
    # spotdl 4.x has no env-var support for credentials, so they go through argv. Output is captured and
    # printed (redacted) only on failure.
    cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    cmd = [sys.executable, "-m", "spotdl", args[0], *args[1:], "--client-id", cid, "--client-secret", secret]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join(redact(r.stdout + r.stderr, [cid, secret]).strip().splitlines()[-5:])
        raise IngestError(f"spotdl {args[0]} failed with exit code {r.returncode}: {tail}")


def fetch_cover(url: str, dest: Path) -> Path:
    if urllib.parse.urlparse(url).scheme != "https":
        raise IngestError("cover url must be https")
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read(COVER_MAX_BYTES + 1)
    if len(data) > COVER_MAX_BYTES:
        raise IngestError("cover too large")
    dest.write_bytes(data)
    return dest


# ---- per-track pipeline ----

@dataclass
class Deps:
    """Everything process_track needs from the outside world, so tests can pass fakes."""
    find_match: Callable[[dict], object | None]      # song -> match with .video_id, or None
    download: Callable[[dict, object, Path], Path]   # song, match, dir -> mp3 path
    upload_audio: Callable[[dict, Path], dict]       # -> telegram message with "audio" and "message_id"
    upload_cover: Callable[[dict, Path], str]        # -> cover file_id
    d1_batch: Callable[[list], object]
    delete_message: Callable[[int], None]


def process_track(song: dict, deps: Deps, existing: set, covers: dict, work: Path, dry: bool = False) -> str:
    """Returns one of: exists, no_match, too_big, dry_run, new. Raises on any other failure."""
    sid = song.get("song_id") or ""
    if not SPOTIFY_ID.fullmatch(sid):
        raise IngestError("invalid song_id")
    if sid in existing:
        return "exists"
    m = deps.find_match(song)
    if not m:
        return "no_match"
    if dry:
        return "dry_run"
    tdir = work / sid
    tdir.mkdir(exist_ok=True)
    mp3 = deps.download(song, m, tdir)
    size = mp3.stat().st_size
    if too_big(size):
        return "too_big"
    msg = deps.upload_audio(song, mp3)
    try:
        al = album_id(primary_artist(song), song.get("album_name") or UNKNOWN_ALBUM)
        cover = covers.get(al)
        if not cover and song.get("cover_url"):
            try:
                cover = deps.upload_cover(song, tdir)
                covers[al] = cover
            except (IngestError, OSError, KeyError, ValueError):
                print(f"WARN cover failed for {sid}")
        deps.d1_batch(upsert_statements(song, size, msg["audio"]["file_id"], msg["message_id"], cover))
    except Exception:
        try:
            deps.delete_message(msg["message_id"])
        except (IngestError, OSError, KeyError, ValueError):
            print(f"WARN could not delete orphan Telegram message {msg.get('message_id')}")
        raise
    existing.add(sid)
    return "new"


def real_deps(ytm) -> Deps:
    def find(song):
        return find_match(ytm, song["name"], primary_artist(song), song.get("album_name"), int(song["duration"]))

    def download(song, m, tdir):
        spotdl_run(["download", f"https://music.youtube.com/watch?v={m.video_id}|{song['url']}",
                    "--output", str(tdir / "{track-id}.{output-ext}"), "--format", "mp3", "--bitrate", BITRATE],
                   tdir.parent)
        files = list(tdir.glob("*.mp3"))
        if len(files) != 1:
            raise IngestError("expected exactly one mp3 after download")
        return files[0]

    def audio(song, path):
        return tg_upload("sendAudio", "audio", path, "audio/mpeg",
                         {"title": song["name"], "performer": ", ".join(song.get("artists") or []),
                          "duration": int(song["duration"])})

    def cover(song, tdir):
        photo = fetch_cover(song["cover_url"], tdir / "cover.jpg")
        return largest_photo_file_id(tg_upload("sendPhoto", "photo", photo, "image/jpeg")["photo"])

    return Deps(find, download, audio, cover, d1_batch, tg_delete)


def run(songs: list[dict], deps: Deps, existing: set, covers: dict, work: Path, dry: bool) -> dict:
    counts = {"new": 0, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0, "failed": 0}
    for song in dedupe(songs):
        label = f"{(song.get('artists') or ['?'])[0]} - {song.get('name', '?')}"
        try:
            status = process_track(song, deps, existing, covers, work, dry)
        except (IngestError, OSError, KeyError, ValueError, IndexError, TypeError) as e:
            detail = f": {e}" if isinstance(e, IngestError) else ""
            print(f"ERROR {label}: {type(e).__name__}{detail}")
            counts["failed"] += 1
            continue
        counts[status] += 1
        if status in ("no_match", "too_big"):
            print(f"WARN {status.replace('_', ' ')}: {label}")
        elif status in ("new", "dry_run"):
            print(f"{'ingested' if status == 'new' else 'dry-run would ingest'}: {label}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync one public Spotify playlist into the library.")
    ap.add_argument("playlist_url")
    ap.add_argument("--dry-run", action="store_true", help="match only; download and upload nothing")
    args = ap.parse_args()
    tg_check.load_env()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    from ytmusicapi import YTMusic
    existing = {r["spotify_id"] for r in d1_query("SELECT spotify_id FROM tracks")}
    covers = {r["id"]: r["cover_file_id"] for r in d1_query("SELECT id, cover_file_id FROM albums")}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        save = work / "list.spotdl"
        spotdl_run(["save", args.playlist_url, "--save-file", str(save)], work)
        counts = run(json.loads(save.read_text(encoding="utf-8")), real_deps(YTMusic()), existing, covers, work,
                     args.dry_run)
    print(f"done: {counts['new']} new, {counts['exists']} already present, {counts['no_match']} no match, "
          f"{counts['too_big']} too big, {counts['failed']} failed")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

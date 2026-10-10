"""Sync one public Spotify playlist into the library: match -> download -> Telegram -> D1.

Usage: python ingest/ingest.py PLAYLIST_URL [--dry-run]

Env (or ./.env): TG_BOT_TOKEN, TG_CHANNEL_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID. Secrets are never printed.
Tracks whose spotify_id is already in D1 are skipped, so re-running adds nothing.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import tg_check
from match import find_match

MAX_BYTES = 20 * 1024 * 1024  # Telegram Bot API getFile limit
BITRATE = "256k"
REQUIRED_ENV = ["TG_BOT_TOKEN", "TG_CHANNEL_ID", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID"]


# ---- pure logic (unit tested) ----

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
    artist = song.get("album_artist") or song["artists"][0]
    ar, al, tr = artist_id(artist), album_id(artist, song["album_name"]), track_id(song["song_id"])
    return [
        ("INSERT INTO artists (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
         [ar, artist]),
        ("INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES (?, ?, ?, ?, ?) "
         "ON CONFLICT(id) DO UPDATE SET name = excluded.name, year = excluded.year, "
         "cover_file_id = COALESCE(excluded.cover_file_id, albums.cover_file_id)",
         [al, ar, song["album_name"], song.get("year") or None, cover_file_id]),
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

def d1_query(sql: str, params: list | None = None) -> list[dict]:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
           f"/d1/database/{os.environ['D1_DATABASE_ID']}/query")
    req = urllib.request.Request(url, data=json.dumps({"sql": sql, "params": params or []}).encode(), headers={
        "content-type": "application/json", "authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"D1 API error {e.code}")
    if not body.get("success"):
        sys.exit(f"D1 query failed: {[e.get('message') for e in body.get('errors', [])]}")
    return body["result"][0].get("results", [])


def tg_upload(method: str, field: str, path: Path, content_type: str, extra: dict | None = None) -> dict:
    boundary = os.urandom(16).hex()
    fields = {"chat_id": os.environ["TG_CHANNEL_ID"], **(extra or {})}
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
                  f'Content-Type: {content_type}\r\n\r\n').encode())
    parts += [path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    req = urllib.request.Request(f"{tg_check.API}/bot{os.environ['TG_BOT_TOKEN']}/{method}", data=b"".join(parts),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    return tg_check._send(req)["result"]


def spotdl_run(args: list[str], cwd: Path) -> None:
    cmd = [sys.executable, "-m", "spotdl", args[0], *args[1:],
           "--client-id", os.environ["SPOTIFY_CLIENT_ID"], "--client-secret", os.environ["SPOTIFY_CLIENT_SECRET"]]
    # Output is captured: spotdl may echo its arguments, which include the Spotify credentials.
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"spotdl {args[0]} failed with exit code {r.returncode}")


def fetch_cover(url: str, dest: Path) -> Path:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("cover url must be https")
    with urllib.request.urlopen(url, timeout=60) as r:
        dest.write_bytes(r.read())
    return dest


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        sys.exit(__doc__)
    tg_check.load_env()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    from ytmusicapi import YTMusic
    ytm = YTMusic()
    existing = {r["spotify_id"] for r in d1_query("SELECT spotify_id FROM tracks")}
    covers = {r["id"]: r["cover_file_id"] for r in d1_query("SELECT id, cover_file_id FROM albums")}

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        save = work / "list.spotdl"
        spotdl_run(["save", args[0], "--save-file", str(save)], work)
        songs = json.loads(save.read_text(encoding="utf-8"))
        new = skipped = failed = 0
        for song in songs:
            label = f"{song['artists'][0]} - {song['name']}"
            if song["song_id"] in existing:
                skipped += 1
                continue
            m = find_match(ytm, song["name"], song["artists"][0], song.get("album_name"), int(song["duration"]))
            if not m:
                print(f"WARN no match: {label}")
                failed += 1
                continue
            if dry:
                print(f"dry-run would ingest: {label} <- {m.video_id}")
                continue
            try:
                tdir = work / song["song_id"]
                tdir.mkdir()
                spotdl_run(["download", f"https://music.youtube.com/watch?v={m.video_id}|{song['url']}",
                            "--output", str(tdir / "{track-id}.{output-ext}"), "--format", "mp3",
                            "--bitrate", BITRATE], work)
                files = list(tdir.glob("*.mp3"))
                if len(files) != 1:
                    raise RuntimeError("expected exactly one mp3 after download")
                size = files[0].stat().st_size
                if too_big(size):
                    print(f"WARN too large ({size} bytes), skipped: {label}")
                    failed += 1
                    continue
                msg = tg_upload("sendAudio", "audio", files[0], "audio/mpeg",
                                {"title": song["name"], "performer": ", ".join(song["artists"]),
                                 "duration": int(song["duration"])})
                artist = song.get("album_artist") or song["artists"][0]
                al = album_id(artist, song["album_name"])
                cover = covers.get(al)
                if not cover and song.get("cover_url"):
                    try:
                        photo = fetch_cover(song["cover_url"], tdir / "cover.jpg")
                        cover = largest_photo_file_id(tg_upload("sendPhoto", "photo", photo, "image/jpeg")["photo"])
                        covers[al] = cover
                    except (urllib.error.URLError, ValueError, KeyError) as e:
                        print(f"WARN cover failed for {label}: {type(e).__name__}")
                for sql, params in upsert_statements(song, size, msg["audio"]["file_id"], msg["message_id"], cover):
                    d1_query(sql, params)
                new += 1
                print(f"ingested: {label}")
            except RuntimeError as e:
                print(f"WARN {label}: {e}")
                failed += 1
    print(f"done: {new} new, {skipped} already present, {failed} skipped/failed")


if __name__ == "__main__":
    main()

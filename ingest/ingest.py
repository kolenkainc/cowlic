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
import shutil
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


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def redact(text: str, secrets: list[str]) -> str:
    """Remove secrets from tool output. Matching runs on ANSI-stripped text, ignores case and tolerates
    whitespace/line breaks between any two characters (rich hard-wraps long values when not on a TTY)."""
    text = strip_ansi(text)
    for s in sorted((x for x in secrets if x), key=len, reverse=True):
        text = re.sub(r"\s*".join(re.escape(c) for c in s.strip()), "***", text, flags=re.IGNORECASE)
    return text


def neutralize(text: str) -> str:
    """Stop any line from being read as a GitHub Actions workflow command (`::cmd::` or legacy `##[cmd]`)."""
    return "\n".join(f"[neutralised] {ln}" if ln.lstrip().startswith(("::", "##[")) else ln
                     for ln in text.splitlines())


def log(msg: str) -> None:
    """The only way ingest prints: external text (titles, tool output) cannot issue workflow commands."""
    print(neutralize(msg))


# rich draws spotdl's tracebacks as boxes whose lines start with these characters; they carry no error text.
BOX_START = "│╭╰├┃┏┗─"
# `[01:32:59 PM] DEBUG MainThread - <message> base.py:408`: spotdl's DEBUG record (whitespace already collapsed).
# spotdl 4.5.2 get_download_metadata() logs the yt-dlp exception this way (logger.debug(exception)).
DEBUG_RECORD = re.compile(r"^\[[^\]]+\]\s+DEBUG\s+\S+ - (?P<msg>.*?)(?:\s+\S+\.py:\d+)?$")
EXC_SUMMARY = re.compile(r"^(?:[\w.]+\.)?\w*(?:Error|Exception): \S")  # last line of a printed Python traceback
MAX_CAUSES = 3
GENERIC_FAILURE = "YT-DLP download error"  # spotdl's own wrapper message; it names no cause
URL_QUERY = re.compile(r"(?<=\S)[?#]\S+")  # query string or fragment of a URL: signed googlevideo links carry ip, sig, pot
OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_+=-]{24,}")  # long base64/hex/url-safe run: a token, not readable text


def scrub_cause(ln: str) -> str:
    """A cause line is public log text: drop URL query strings and fragments and mask long opaque tokens."""
    return OPAQUE_TOKEN.sub("<token>", URL_QUERY.sub("?<stripped>", ln))


def clean_lines(text: str, secrets: list[str]) -> list[str]:
    """Output lines, redacted first, then whitespace-collapsed (rich pads lines to the console width), with
    empty lines, debug noise and rich traceback-box lines dropped and each line capped at LINE_MAX."""
    lines = []
    for ln in redact(text, secrets).splitlines():
        ln = " ".join(ln.split())
        if ln and ln[0] not in BOX_START and not any(n in ln for n in SPOTDL_DEBUG_NOISE):
            lines.append(ln[:LINE_MAX])
    return lines


def cause_lines(lines: list[str]) -> list[str]:
    """Lines that carry the real download error, searched in the WHOLE output (they can be far from its end):
    spotdl's DEBUG record of the yt-dlp exception (`ERROR: [youtube] ...`) and printed exception summaries,
    scrubbed of query strings and opaque tokens, without repeats; the last MAX_CAUSES are kept."""
    found = []
    for ln in lines:
        m = DEBUG_RECORD.match(ln)
        if (m and (m["msg"].startswith("ERROR:") or EXC_SUMMARY.match(m["msg"]))) or EXC_SUMMARY.match(ln):
            ln = scrub_cause(ln)
            if ln not in found and GENERIC_FAILURE not in ln:
                found.append(ln)
    return found[-MAX_CAUSES:]


def tail_of(text: str, secrets: list[str]) -> str:
    """Last TAIL_LINES meaningful lines of tool output (see clean_lines)."""
    return "\n".join(clean_lines(text, secrets)[-TAIL_LINES:])


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

SPOTDL_HIDDEN_ENV = {"TG_BOT_TOKEN", "TG_CHANNEL_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                     "D1_DATABASE_ID"}
TAIL_LINES = 15  # lines of spotdl output kept in an error, so a YouTube block stays diagnosable
LINE_MAX = 400
# spotdl hides the real yt-dlp error (logger.debug(exception)) below DEBUG, and always prints its "Some YouTube
# downloads require Deno" warning for ANY failed download, so downloads run at DEBUG with a wide console (rich
# wraps at COLUMNS) to capture the cause. DEBUG also dumps the whole settings dict, which is dropped here.
SPOTDL_DOWNLOAD_ARGS = ["--log-level", "DEBUG"]
SPOTDL_CONSOLE_COLUMNS = "2000"
SPOTDL_DEBUG_NOISE = ("Downloader settings:",)
MIN_DENO = (2, 3, 0)  # yt-dlp 2026.8.19: DenoJsRuntime.MIN_SUPPORTED_VERSION
SPOTDL_CMD = [sys.executable, "-m", "spotdl"]  # tests replace this with a fake


def spotdl_run(args: list[str], cwd: Path) -> tuple[list[str], str]:
    """Runs spotdl; returns (cause lines, redacted tail of its output <= TAIL_LINES). spotdl 4.5.2 exits 0 even
    when a song's download failed (e.g. a YouTube block), so callers include both when no file appeared."""
    # spotdl 4.x has no env-var support for credentials, so they go through argv. Output is captured and only
    # ever surfaced redacted.
    cid, secret = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
    cmd = [*SPOTDL_CMD, args[0], *args[1:], "--client-id", cid, "--client-secret", secret]
    # spotdl needs none of the Telegram / Cloudflare values, so keep them out of its environment.
    env = {k: v for k, v in os.environ.items() if k not in SPOTDL_HIDDEN_ENV}
    env["COLUMNS"] = SPOTDL_CONSOLE_COLUMNS
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    lines = clean_lines(r.stdout + "\n" + r.stderr, [cid, secret])
    tail = "\n".join(lines[-TAIL_LINES:])
    if r.returncode != 0:
        raise IngestError(f"spotdl {args[0]} failed with exit code {r.returncode}: {tail}")
    return cause_lines(lines), tail


def js_runtime_problem() -> str | None:
    """Why yt-dlp cannot use Deno (its default JavaScript runtime for YouTube), or None when it looks usable.
    spotdl's own warning cannot be used for this: it is printed for every failed download."""
    exe = shutil.which("deno")
    if not exe:
        return "deno not found on PATH"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "deno --version failed"
    m = re.match(r"deno (\d+)\.(\d+)\.(\d+)", r.stdout)
    if not m:
        return "cannot read the deno version"
    version = tuple(int(x) for x in m.groups())
    if version < MIN_DENO:
        return f"deno {'.'.join(map(str, version))} is older than {'.'.join(map(str, MIN_DENO))}"
    return None


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
                log(f"WARN cover failed for {sid}")
        deps.d1_batch(upsert_statements(song, size, msg["audio"]["file_id"], msg["message_id"], cover))
    except Exception:
        try:
            deps.delete_message(msg["message_id"])
        except (IngestError, OSError, KeyError, ValueError):
            log(f"WARN could not delete orphan Telegram message {msg.get('message_id')}")
        raise
    existing.add(sid)
    return "new"


def real_deps(ytm) -> Deps:
    def find(song):
        return find_match(ytm, song["name"], primary_artist(song), song.get("album_name"), int(song["duration"]))

    def download(song, m, tdir):
        causes, tail = spotdl_run(["download", f"https://music.youtube.com/watch?v={m.video_id}|{song['url']}",
                           "--output", str(tdir / "{track-id}.{output-ext}"), "--format", "mp3", "--bitrate", BITRATE,
                            *SPOTDL_DOWNLOAD_ARGS],
                          tdir.parent)
        files = list(tdir.glob("*.mp3"))
        if len(files) != 1:
            # causes first and on the first line: the CI summary lists only the first line of each ERROR
            raise IngestError("expected exactly one mp3 after download"
                              + (f"; yt-dlp error: {' | '.join(causes)}" if causes else "")
                              + (f"; spotdl output (last lines):\n{tail}" if tail else ""))
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
            log(f"ERROR {label}: {type(e).__name__}{detail}")
            counts["failed"] += 1
            continue
        counts[status] += 1
        if status == "exists":
            log(f"skipped (already present): {label}")
        elif status in ("no_match", "too_big"):
            log(f"WARN {status.replace('_', ' ')}: {label}")
        elif status in ("new", "dry_run"):
            log(f"{'ingested' if status == 'new' else 'dry-run would ingest'}: {label}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync one public Spotify playlist into the library.")
    ap.add_argument("playlist_url")
    ap.add_argument("--dry-run", action="store_true", help="match only; download and upload nothing")
    args = ap.parse_args()
    tg_check.load_env()
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log(f"ERROR missing env vars: {', '.join(missing)}")
        return 1

    problem = js_runtime_problem()
    if problem:
        log(f"WARN js runtime: {problem}; yt-dlp needs Deno >= {'.'.join(map(str, MIN_DENO))} for YouTube")

    from ytmusicapi import YTMusic
    existing = {r["spotify_id"] for r in d1_query("SELECT spotify_id FROM tracks")}
    covers = {r["id"]: r["cover_file_id"] for r in d1_query("SELECT id, cover_file_id FROM albums")}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        save = work / "list.spotdl"
        try:
            spotdl_run(["save", args.playlist_url, "--save-file", str(save)], work)
        except IngestError as e:
            log(f"ERROR spotdl save failed: {e}")
            return 1
        counts = run(json.loads(save.read_text(encoding="utf-8")), real_deps(YTMusic()), existing, covers, work,
                     args.dry_run)
    log(f"done: {counts['new']} new, {counts['exists']} already present, {counts['no_match']} no match, "
          f"{counts['too_big']} too big, {counts['failed']} failed")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Sync public Spotify playlists into the library: match -> download -> Telegram -> D1.

Usage: python ingest/ingest.py (PLAYLIST_URL | --sources sources.yml) [--dry-run] [--result-json PATH]
                               [--max-new N] [--max-minutes M] [--state-file PATH]
                               [--retry-no-match-days D] [--retry-failed-days D]
                               [--pace-seconds S] [--pace-jitter-seconds J] [--retry-attempts N]
One playlist failing does not stop the others; the exit code is 1 if any playlist or track failed.
sources.yml may also have a `followed_artists:` section (#121): the tracks of every artist the owner follows on
Spotify are ingested too, no playlist is created. Needs SPOTIFY_USER_REFRESH_TOKEN (python ingest/spotify_user.py
login); without a usable login the run ends with exit code 2 before doing any work.
Limits (off unless given) and the local state file (cooldown for no_match/failed tracks): docs/local-ingest.md.

Env (or ./.env): TG_BOT_TOKEN, TG_CHANNEL_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
INGEST_CLOUDFLARE_API_TOKEN (narrow, D1:Edit only), CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID. Secrets are never
printed. Ingest never reads the broad CLOUDFLARE_API_TOKEN (that one is for wrangler/ad-hoc use, outside ingest).
The .env syntax is documented in docs/architecture.md section 4.
Tracks whose spotify_id is already in D1 are skipped, so re-running adds nothing.
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tg_check
from match import MODE_TITLE_ONLY, find_match

MAX_BYTES = 20 * 1024 * 1024  # Telegram Bot API getFile limit
BITRATE = "256k"
REQUIRED_ENV = ["TG_BOT_TOKEN", "TG_CHANNEL_ID", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
                "INGEST_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID"]


SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
# a song id is a Spotify id or a synthetic Last.fm id: "lfm:" + 16 lowercase hex (built by the Last.fm source, #149)
SONG_ID = re.compile(r"(?:[A-Za-z0-9]{22}|lfm:[0-9a-f]{16})")
LASTFM_ID_PREFIX = "lfm:"
COVER_MAX_BYTES = 10 * 1024 * 1024
UNKNOWN_ARTIST, UNKNOWN_ALBUM = "Unknown Artist", "Unknown Album"


class IngestError(Exception):
    """Per-track failure with a message that is safe to print (no tokens, no URLs with tokens)."""


class YouTubeSignInRequired(IngestError):
    """YouTube answered with its bot check ("Sign in to confirm you're not a bot"). Not a per-track failure: it ends
    the whole run (no retry, no bypass), see run() and main(). run()/run_playlists() log the counts and attach
    them here (counts of the playlist in progress, tracks and playlists not attempted) for the status file (#31)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.counts: dict | None = None  # counts of the playlist in progress, challenged track not included
        self.tracks_not_attempted = 0    # tracks after the challenged one in the playlist in progress
        self.playlists_not_attempted = 0  # playlists after the one in progress (not even fetched)
        self.results: dict = {}          # url -> counts of the playlists finished before the one in progress


# Distinct process exit code and failure class for this outcome (1 = other failures, 2 = bad usage).
EXIT_YOUTUBE_SIGN_IN = 3
EXIT_CONFIG = 2  # the followed_artists source cannot run: no or invalid Spotify user login (#121); nothing ran
FAILURE_YOUTUBE_SIGN_IN = "youtube-sign-in"
# Owner decision (#62): the bot check is only DETECTED and reported. No cookies, proxies, PO-token tricks or client
# rotation are added or planned; the owner waits and re-runs later.
# yt-dlp's wording, as captured in the cause lines: "ERROR: [youtube] <id>: Sign in to confirm you're not a bot.
# Use --cookies-from-browser ...". The apostrophe may be straight, curly or missing.
SIGN_IN_CHALLENGE = re.compile(r"sign in to confirm (?:that )?you.?re not a bot", re.IGNORECASE)


def is_sign_in_challenge(lines: list[str]) -> bool:
    return any(SIGN_IN_CHALLENGE.search(ln) for ln in lines)


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


ERROR_BODY_MAX = 4096  # bytes read from an HTTP error response; the rest is never read
REASON_MAX = 200  # characters of an upstream error message that may reach an error text


def read_error_body(fp) -> dict:
    """Parsed JSON object of an HTTP error response (read bounded to ERROR_BODY_MAX bytes). A body that is not a
    JSON object becomes {"error": {"message": <its text>}} so error_reason() can show a short, bounded reason."""
    raw = fp.read(ERROR_BODY_MAX)
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    text = raw.decode("utf-8", "replace").strip()
    return {"error": {"message": text}} if text else {}


def error_reason(body: dict, secrets: list[str]) -> str:
    """The reason an API gave for an error (Spotify: error.message; OAuth style: a string `error`), safe to show:
    redacted first (so a secret is removed whole before any cutting), control characters replaced by '?', cut to
    REASON_MAX characters. Empty when the body carries no string reason."""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    if not isinstance(msg, str):
        return ""
    text = "".join(c if c.isprintable() else "?" for c in redact(msg, secrets)).strip()
    return text if len(text) <= REASON_MAX else text[:REASON_MAX] + "..."


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
ENCODED_DELIM = re.compile(r"%3[Ff]|%23")  # percent-encoded ? and #: decoded so the query is recognised and stripped
URL_QUERY = re.compile(r"(?<=\S)[?#]\S+")  # query string or fragment of a URL: signed googlevideo links carry ip, sig, pot
VIDEO_ID_PARAM = re.compile(r"(?:^|&)v=([A-Za-z0-9_-]{1,20})(?=&|$)")  # YouTube watch id: public, kept
IP_PARAM = re.compile(r"(?i)(?<![A-Za-z0-9])ip=[^\s&;/]+")  # ip=1.2.3.4 or ;ip=...
_H = r"[0-9A-Fa-f]{1,4}"
IPV4 = re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])")
IPV6 = re.compile(  # full form, or compressed form (it contains "::"); a bare 13:10:00 timestamp is not matched
    rf"(?<![\w:.-])(?:(?:{_H}:){{7}}{_H}|(?:{_H}:){{1,7}}:(?:{_H}(?::{_H}){{0,6}})?|::{_H}(?::{_H}){{0,6}})(?![\w:.-])"
)
SECRET_PARAM = re.compile(r"(?i)\b(sig|lsig|pot|signature|token|key)=[^\s&;]+")  # also on a wrapped URL's 2nd line
OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_+=-]{24,}")  # long base64/hex/url-safe run: a token, not readable text
# the lookbehind makes a run start only at its beginning: without it a long run is rescanned from every position
JOINED_RUN = re.compile(r"(?<![A-Za-z0-9_+=-])[A-Za-z0-9_+=-]+(?:[./][A-Za-z0-9_+=-]+)+")  # pieces joined by . or / (JWT, signed paths)


def _query(m: re.Match) -> str:
    text = m.group(0)
    if text[0] != "?":
        return "?<stripped>"
    v = VIDEO_ID_PARAM.search(text[1:])
    return f"?v={v[1]}&<stripped>" if v and "&" in text else (f"?v={v[1]}" if v else "?<stripped>")


def _opaque_piece(piece: str) -> bool:
    return len(piece) >= 8 and any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece)


def _joined(m: re.Match) -> str:
    run = m.group(0)
    pieces = re.split(r"[./]", run)
    # hostnames and readable paths stay; a run whose opaque-looking pieces add up to 16+ characters is a token
    if len(run) >= 24 and sum(len(x) for x in pieces if _opaque_piece(x)) >= 16:
        return "<token>"
    return run


def scrub_cause(ln: str) -> str:
    """Log text is public: drop URL query strings and fragments (keeping a YouTube v=<id>), mask ip= values and
    bare IPv4/IPv6 addresses, and mask long opaque tokens (also ones joined by . or /)."""
    ln = ln.replace("&amp;", "&")  # HTML-escaped separators
    ln = ENCODED_DELIM.sub(lambda m: "?" if m[0][2] in "Ff" else "#", ln)
    ln = URL_QUERY.sub(_query, ln)
    ln = SECRET_PARAM.sub(r"\1=<stripped>", ln)
    ln = IP_PARAM.sub("ip=<ip>", ln)
    ln = IPV6.sub("<ip>", IPV4.sub("<ip>", ln))
    ln = JOINED_RUN.sub(_joined, ln)
    return OPAQUE_TOKEN.sub("<token>", ln)


def clean_lines(text: str, secrets: list[str]) -> list[str]:
    """Output lines, redacted first, then whitespace-collapsed (rich pads lines to the console width), with
    empty lines, debug noise and rich traceback-box lines dropped, URLs and tokens scrubbed (scrub_cause) and
    each line capped at LINE_MAX. Every log path (tail, cause lines, summaries) goes through here."""
    lines = []
    for ln in redact(text, secrets).splitlines():
        ln = " ".join(ln.split())
        if ln and ln[0] not in BOX_START and not any(n in ln for n in SPOTDL_DEBUG_NOISE):
            lines.append(scrub_cause(ln[:LINE_MAX * 4])[:LINE_MAX])  # pre-cap bounds regex work
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


# Spotify playlist or album URL: https only, fixed host, 22-char id, optional harmless query (?si=...).
PLAYLIST_URL = re.compile(r"https://open\.spotify\.com/(?:playlist|album)/[A-Za-z0-9]{22}(?:\?[A-Za-z0-9=&_.%-]{1,200})?")
SOURCES_ITEM = re.compile(r"-[ \t]+(\S+)")


def validate_playlist_url(url: str) -> str:
    """Allowlist check shared by the CLI argument and sources.yml; rejects, never rewrites."""
    if not PLAYLIST_URL.fullmatch(url):
        raise IngestError(f"not a Spotify playlist/album URL (https://open.spotify.com/playlist/<22 chars>): {url[:120]!r}")
    return url


@dataclass(frozen=True)
class FollowedConfig:
    """The `followed_artists:` section of sources.yml (see docs/local-ingest.md). Defaults apply to absent keys."""
    enabled: bool = True
    max_artists: int | None = None      # artists started per run; None = no limit
    rescan_days: int = 7                # a fully scanned artist is looked at again after this many days (0 = every run)
    groups: tuple[str, ...] = ("album", "single")
    market: str = "US"
    exclude: tuple[str, ...] = ()       # artist ids that are never ingested


@dataclass(frozen=True)
class SourceConfig:
    urls: list[str]
    followed: FollowedConfig | None     # None: no section (a plain playlist file)


FOLLOWED_HEADER = "followed_artists:"
FOLLOWED_KEY = re.compile(r"  ([a-z_]+):(?: (\S.*))?")  # exactly two spaces of indent
FOLLOWED_ITEM = re.compile(r"    - (\S+)")              # exactly four spaces, under `exclude:`
FOLLOWED_KEYS = ("enabled", "max_artists", "rescan_days", "groups", "market", "exclude")
ARTIST_ID = re.compile(r"[A-Za-z0-9]{22}")
FOLLOWED_GROUPS = ("album", "single", "compilation", "appears_on")  # ingest/artist_tracks.py ALL_GROUPS (tested)
INT_VALUE = re.compile(r"[0-9]{1,5}")


def _followed_value(key: str, value: str):
    """Typed value of one `key: value` line of the followed_artists section; rejects, never rewrites."""
    if key == "enabled":
        if value not in ("true", "false"):
            raise ValueError("enabled must be true or false")
        return value == "true"
    if key in ("max_artists", "rescan_days"):
        low, high = (1, 10000) if key == "max_artists" else (0, 3650)
        if not INT_VALUE.fullmatch(value) or not low <= int(value) <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
        return int(value)
    if key == "groups":
        items = value.split(",")
        if any(g not in FOLLOWED_GROUPS for g in items) or len(set(items)) != len(items):
            raise ValueError(f"groups must be a comma-separated list without repeats from {','.join(FOLLOWED_GROUPS)}")
        return tuple(items)
    if not re.fullmatch(r"[A-Z]{2}", value):  # market
        raise ValueError("market must be an upper-case ISO 3166-1 alpha-2 code such as US")
    return value


def parse_source_config(text: str) -> SourceConfig:
    """Strict parser for sources.yml. Playlist part: a flat list, one `- <url>` per line (a leading indent is
    tolerated outside the section); blank lines and whole-line `#` comments are ignored, anything else is an error
    (no silent skip). Every URL passes validate_playlist_url. Optional section, at most once, header at column 0:

        followed_artists:
          enabled: true          (two-space indent; keys: enabled max_artists rescan_days groups market exclude)
          max_artists: 20
          exclude:
            - <22-char artist id>    (four-space indent)

    The section ends at the next line without indent. Unknown or repeated keys, a malformed value, a repeated
    excluded id and anything else is an error. A file without the section behaves exactly as before."""
    urls: list[str] = []
    fields: dict = {}
    exclude: list[str] = []
    seen_section = in_section = in_exclude = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_section and line[0] in " \t":
            where = f"sources line {no} (followed_artists)"
            if m := FOLLOWED_ITEM.fullmatch(line):
                if not in_exclude:
                    raise IngestError(f"{where}: a list item is only allowed under 'exclude:'")
                if not ARTIST_ID.fullmatch(m[1]):
                    raise IngestError(f"{where}: not a Spotify artist id (22 letters/digits): {m[1][:40]!r}")
                if m[1] in exclude:
                    raise IngestError(f"{where}: artist id listed twice")
                exclude.append(m[1])
                continue
            m = FOLLOWED_KEY.fullmatch(line)
            if not m:
                raise IngestError(f"{where}: expected '  <key>: <value>' (two spaces) or '    - <artist id>'")
            key, value = m[1], m[2]
            if key not in FOLLOWED_KEYS:
                raise IngestError(f"{where}: unknown key {key!r} (allowed: {', '.join(FOLLOWED_KEYS)})")
            if key in fields:
                raise IngestError(f"{where}: key {key!r} given twice")
            in_exclude = key == "exclude"
            if in_exclude:
                if value is not None:
                    raise IngestError(f"{where}: 'exclude:' takes a list on the following lines ('    - <artist id>')")
                fields[key] = ()
            elif value is None:
                raise IngestError(f"{where}: key {key!r} has no value")
            else:
                try:
                    fields[key] = _followed_value(key, value)
                except ValueError as e:
                    raise IngestError(f"{where}: {e}") from None
            continue
        in_section = in_exclude = False
        if line == FOLLOWED_HEADER:
            if seen_section:
                raise IngestError(f"sources line {no}: section 'followed_artists' given twice")
            seen_section = in_section = True
            continue
        m = SOURCES_ITEM.fullmatch(line.strip())
        if not m:
            raise IngestError(f"sources line {no}: expected '- <playlist url>'")
        try:
            urls.append(validate_playlist_url(m[1]))
        except IngestError as e:
            raise IngestError(f"sources line {no}: {e}") from None
    followed = None
    if seen_section:
        fields["exclude"] = tuple(exclude)
        followed = FollowedConfig(**fields)
    if not urls and not (followed and followed.enabled):
        raise IngestError("sources file lists no playlists" + (" and followed_artists is not enabled" if followed else ""))
    return SourceConfig(list(dict.fromkeys(urls)), followed)


def parse_sources(text: str) -> list[str]:
    """The playlist URLs of sources.yml (see parse_source_config)."""
    return parse_source_config(text).urls


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
        "content-type": "application/json", "authorization": f"Bearer {os.environ['INGEST_CLOUDFLARE_API_TOKEN']}"})
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

SPOTDL_HIDDEN_ENV = {"TG_BOT_TOKEN", "TG_CHANNEL_ID", "INGEST_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN",
                     "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID", "SPOTIFY_USER_REFRESH_TOKEN"}
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
        if is_sign_in_challenge(cause_lines(lines) + lines[-TAIL_LINES:]):
            raise YouTubeSignInRequired("YouTube asks to sign in (bot check)")
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


def process_track(song: dict, deps: Deps, existing: set, covers: dict, work: Path, dry: bool = False,
                  notes: dict | None = None) -> str:
    """Returns one of: exists, no_match, too_big, dry_run, new. Raises on any other failure. When the match was
    made without a duration (match.MODE_TITLE_ONLY, #151) `notes["title_only"]` is set and the rest of the pipeline
    sees the song with the duration of the chosen YouTube candidate; a song with a duration is never touched."""
    sid = song.get("song_id") or ""
    if not SONG_ID.fullmatch(sid):
        raise IngestError("invalid song_id")
    if sid in existing:
        return "exists"
    m = deps.find_match(song)
    if not m:
        return "no_match"
    if getattr(m, "mode", None) == MODE_TITLE_ONLY:
        if notes is not None:
            notes["title_only"] = True
        song = {**song, "duration": m.duration_s}
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


STORED_ROWS_SQL = ("SELECT t.title AS title, ar.name AS artist, t.duration_s AS duration_s FROM tracks t "
                   "JOIN albums al ON al.id = t.album_id JOIN artists ar ON ar.id = al.artist_id")


def read_stored_rows(query: Callable[[str], list[dict]] = d1_query) -> list[tuple]:
    """(title, artist, duration_s) of every stored track: one read-only parameterless SELECT, once per run."""
    return [(r["title"], r["artist"], r["duration_s"]) for r in query(STORED_ROWS_SQL)]


def drop_stored_lastfm(records: list[dict], stored_rows: list[tuple]) -> list[dict]:
    """Cross-source dedupe for a feed that holds Last.fm records (song_id lfm:...): those already stored under another
    id (a Spotify playlist track) are dropped, Spotify-id records pass untouched to the `existing` check of
    process_track. Order is kept. Logs counts only. Call contract for the Last.fm source (not wired yet)."""
    import dedupe as dedupe_mod
    lfm = [r for r in records if str(r.get("song_id", "")).startswith(LASTFM_ID_PREFIX)]
    if not lfm:
        return records
    res = dedupe_mod.drop_stored(lfm, stored_rows)
    gone = {id(r) for r in lfm} - {id(r) for r in res.new}
    log(f"lastfm: {res.stored} of {res.listed} already stored under another id"
        f" ({res.stored_without_duration} matched without a duration)")
    return [r for r in records if id(r) not in gone]


def real_deps(ytm, throttle: "Throttle | None" = None) -> Deps:
    def find(song):
        d = song.get("duration")  # None / missing / 0: the title-only matcher mode (#151); else exactly as before
        return find_match(ytm, song["name"], primary_artist(song), song.get("album_name"), int(d) if d else None)

    def download(song, m, tdir):
        causes, tail = spotdl_run(["download", f"https://music.youtube.com/watch?v={m.video_id}|{song['url']}",
                           "--output", str(tdir / "{track-id}.{output-ext}"), "--format", "mp3", "--bitrate", BITRATE,
                            *SPOTDL_DOWNLOAD_ARGS],
                          tdir.parent)
        files = list(tdir.glob("*.mp3"))
        if len(files) != 1:
            if is_sign_in_challenge(causes):
                raise YouTubeSignInRequired("YouTube asks to sign in (bot check)")
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

    return harden_deps(Deps(find, download, audio, cover, d1_batch, tg_delete), throttle)


# ---- pacing and backoff (#120) ----
# Documented facts (assessment #92 section 4): about 300 videos per hour for a guest YouTube session, a pause of
# 5-10 s between downloads recommended, HTTP 429 means "blocked for overuse" and is usually a soft block. So:
# a pause between downloads (default 5 s + up to 5 s of jitter) and exponential backoff with a cap and a bounded
# number of attempts for TRANSIENT errors only (HTTP 429/5xx, network errors of the YouTube Music search). The
# YouTube sign-in challenge is never retried and never paced around: it ends the run (YouTubeSignInRequired).
# Nothing here bypasses a YouTube check: no cookies, proxies, PO tokens or client rotation (#62).
DEFAULT_PACE_SECONDS = 5.0
DEFAULT_PACE_JITTER_SECONDS = 5.0
DEFAULT_ATTEMPTS = 3          # attempts in total per operation (1 try + 2 retries)
BACKOFF_BASE_SECONDS = 10.0   # waits before retry n: base * 2**(n-1), at most BACKOFF_CAP_SECONDS
BACKOFF_CAP_SECONDS = 120.0
TRANSIENT_TEXT = re.compile(
    r"(?i)HTTP(?: Error| status:)?:? ?(?:429|5\d\d)\b|too many requests|service unavailable|bad gateway|gateway time-?out")


def transient_text(e: Exception) -> bool:
    """429/5xx in the (already redacted) message of spotdl, yt-dlp or ytmusicapi."""
    return bool(TRANSIENT_TEXT.search(str(e)))


def transient_search(e: Exception) -> bool:
    """YouTube Music search: HTTP 429/5xx or a network error (requests errors are OSErrors)."""
    return isinstance(e, OSError) or transient_text(e)


class Throttle:
    """Pause between downloads and bounded exponential backoff. `sleep`, `clock` (monotonic) and `rng` are
    injectable so tests run instantly. pace 0 and jitter 0 disable the pause; attempts 1 disables retries."""

    def __init__(self, pace_seconds: float = DEFAULT_PACE_SECONDS, jitter_seconds: float = DEFAULT_PACE_JITTER_SECONDS,
                 attempts: int = DEFAULT_ATTEMPTS, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] | None = None):
        self.pace, self.jitter, self.attempts, self.base, self.cap = pace_seconds, jitter_seconds, attempts, base, cap
        self.sleep = sleep or time.sleep
        self.clock = clock
        self.rng = rng or random.random
        self.last_download_end: float | None = None

    def pause_before_download(self) -> None:
        """Wait until pace (+ jitter) seconds have passed since the previous download ended; none before the first."""
        if self.last_download_end is None:
            return
        gap = self.pace + (self.rng() * self.jitter if self.jitter else 0.0)
        wait = gap - (self.clock() - self.last_download_end)
        if wait > 0:
            self.sleep(wait)

    def download_finished(self) -> None:
        self.last_download_end = self.clock()

    def call(self, fn: Callable[[], object], transient: Callable[[Exception], bool], what: str):
        """fn() with up to `attempts` tries; only errors `transient` accepts are retried, after base*2**n seconds
        (capped). The sign-in challenge is never retried. The last error is raised unchanged."""
        attempt = 1
        while True:
            try:
                return fn()
            except YouTubeSignInRequired:
                raise
            except Exception as e:  # re-raised below unless transient; nothing is swallowed
                if attempt >= self.attempts or not transient(e):
                    raise
                wait = min(self.cap, self.base * 2 ** (attempt - 1))
                log(f"transient error in {what} ({type(e).__name__}), attempt {attempt} of {self.attempts}; "
                    f"retrying in {wait:g} s")
                self.sleep(wait)
                attempt += 1


def harden_deps(deps: Deps, throttle: Throttle | None) -> Deps:
    """Deps whose search retries transient errors and whose download is paced and retried (see Throttle)."""
    if throttle is None:
        return deps

    def find(song):
        return throttle.call(lambda: deps.find_match(song), transient_search, "YouTube Music search")

    def download(song, m, tdir):
        throttle.pause_before_download()
        try:
            return throttle.call(lambda: deps.download(song, m, tdir), transient_text, "download")
        finally:
            throttle.download_finished()

    return Deps(find, download, deps.upload_audio, deps.upload_cover, deps.d1_batch, deps.delete_message)


# ---- per-run limits and local state (#119) ----

def atomic_write_json(path: Path, obj: object) -> None:
    """Temp file in the same directory (fsynced) + rename, mode 0600, parent directory 0700 when created. On any
    failure the previous file is untouched and the temp file is removed; raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
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


STATE_VERSION = 1
STATE_MAX_BYTES = 50 * 1024 * 1024  # a bigger file is not ours: treated as corrupted
COOLDOWN_STATUSES = ("no_match", "failed")
DEFAULT_RETRY_NO_MATCH_DAYS = 30  # a track YouTube Music has no match for rarely gains one within weeks
DEFAULT_RETRY_FAILED_DAYS = 3     # failures (YouTube, Telegram, D1 hiccups) are more often transient


def default_state_path() -> Path:
    """Next to the wrapper's log: ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/ingest-state.json."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "cowlic" / "ingest-state.json"


def utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


class RunState:
    """Local, gitignored memory of tracks not to retry yet: {spotify_id: {status, retry_after}}, plus (#121) the
    day each followed artist was last scanned to the end. Only ids, a status word and dates are stored (no titles).
    Never raises: a missing or corrupted file starts empty (WARN), a failed write is logged once and
    the previous file stays (atomic_write_json). Written after every change, so a killed run keeps what it learned."""

    def __init__(self, path: Path | None = None, retry_days: dict | None = None,
                 today: Callable[[], datetime.date] = utc_today):
        self.path = path
        days = retry_days or {}
        self.retry_days = {"no_match": days.get("no_match", DEFAULT_RETRY_NO_MATCH_DAYS),
                           "failed": days.get("failed", DEFAULT_RETRY_FAILED_DAYS)}
        self.today = today
        self.entries: dict[str, dict] = {}
        self.artists: dict[str, datetime.date] = {}  # followed artist id -> day of its last completed scan (#121)
        self._write_warned = False

    @classmethod
    def load(cls, path: Path, retry_days: dict | None = None,
             today: Callable[[], datetime.date] = utc_today) -> "RunState":
        st = cls(path, retry_days, today)
        try:
            with open(path, "rb") as f:
                raw = f.read(STATE_MAX_BYTES + 1)
        except FileNotFoundError:
            return st
        except OSError as e:
            log(f"WARN state file unreadable ({type(e).__name__}); starting with an empty state")
            return st
        try:
            if len(raw) > STATE_MAX_BYTES:
                raise ValueError("too big")
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION or not isinstance(data.get("tracks"), dict):
                raise ValueError("unexpected structure")
        except (ValueError, UnicodeDecodeError, RecursionError):
            log("WARN state file is corrupted or not understood; starting with an empty state "
                "(it is replaced at the next change)")
            return st
        dropped = 0
        for sid, e in data["tracks"].items():
            try:
                due = datetime.date.fromisoformat(e["retry_after"])
                ok = SONG_ID.fullmatch(sid) and e["status"] in COOLDOWN_STATUSES
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok:
                dropped += 1
            elif due > st.today():  # expired entries are dropped: the track is tried again
                st.entries[sid] = {"status": e["status"], "retry_after": due.isoformat()}
        raw_artists = data.get("artists", {})
        if not isinstance(raw_artists, dict):
            raw_artists, dropped = {}, dropped + 1
        for aid, day in raw_artists.items():
            try:
                if not ARTIST_ID.fullmatch(aid):
                    raise ValueError("id")
                st.artists[aid] = datetime.date.fromisoformat(day)
            except (TypeError, ValueError):
                dropped += 1
        if dropped:
            log(f"WARN state file: ignored {dropped} malformed entr{'y' if dropped == 1 else 'ies'}")
        return st

    def in_cooldown(self, sid: str) -> bool:
        e = self.entries.get(sid)
        return bool(e) and datetime.date.fromisoformat(e["retry_after"]) > self.today()

    def record(self, sid: str, status: str) -> None:
        """Remember a no_match/failed track; a retry period of 0 days remembers nothing."""
        days = self.retry_days[status]
        if days <= 0 or not SONG_ID.fullmatch(sid):
            return
        self.entries[sid] = {"status": status, "retry_after": (self.today() + datetime.timedelta(days=days)).isoformat()}
        self.save()

    def clear(self, sid: str) -> None:
        if self.entries.pop(sid, None) is not None:
            self.save()

    def artist_due(self, aid: str, rescan_days: int) -> bool:
        """True when the artist was never fully scanned, or its last scan is rescan_days or more days ago (a scan
        dated in the future, after a clock change, counts as due)."""
        done = self.artists.get(aid)
        return done is None or done > self.today() or (self.today() - done).days >= rescan_days

    def record_artist(self, aid: str) -> None:
        """Remember that the artist was scanned to the end today."""
        self.artists[aid] = self.today()
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            data: dict = {"version": STATE_VERSION, "tracks": self.entries}
            if self.artists:  # absent for a playlist-only run, so its file is unchanged
                data["artists"] = {aid: d.isoformat() for aid, d in self.artists.items()}
            atomic_write_json(self.path, data)
        except OSError as e:
            if not self._write_warned:  # once per run; the in-memory state still applies to this run
                log(f"WARN could not write the state file ({type(e).__name__}); cooldowns may be lost")
                self._write_warned = True


class RunLimits:
    """Per-run budget: --max-new N counts tracks attempted in this run (new, too_big, failed, dry-run; not exists,
    cooldown or no_match, where no download was tried) and --max-minutes M is wall clock. None = unlimited.
    stop_reason is None | "cap" | "time"; stopped_url and not_attempted say where it happened."""

    def __init__(self, max_new: int | None = None, max_minutes: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.max_new, self.clock = max_new, clock
        self.deadline = None if max_minutes is None else clock() + max_minutes * 60
        self.attempted = 0
        self.stop_reason: str | None = None
        self.stopped_url: str | None = None
        self.not_attempted = 0  # tracks of the playlist in progress that were not looked at

    def exhausted(self) -> str | None:
        if self.max_new is not None and self.attempted >= self.max_new:
            return "cap"
        if self.deadline is not None and self.clock() >= self.deadline:
            return "time"
        return None

    def stop(self, reason: str, not_attempted: int = 0) -> None:
        self.stop_reason, self.not_attempted = reason, not_attempted


def run(songs: list[dict], deps: Deps, existing: set, covers: dict, work: Path, dry: bool,
        limits: RunLimits | None = None, state: RunState | None = None) -> dict:
    counts = {"new": 0, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0, "failed": 0, "cooldown": 0}
    unique = dedupe(songs)
    for i, song in enumerate(unique):
        label = f"{(song.get('artists') or ['?'])[0]} - {song.get('name', '?')}"
        sid = song.get("song_id") or ""
        if sid not in existing:
            if state and state.in_cooldown(sid):  # does not use the cap
                counts["cooldown"] += 1
                log(f"skipped (cooldown, retry later): {label}")
                continue
            reason = limits.exhausted() if limits else None
            if reason:
                limits.stop(reason, len(unique) - i)
                break
        notes: dict = {}
        try:
            status = process_track(song, deps, existing, covers, work, dry, notes)
        except YouTubeSignInRequired as e:
            e.counts, e.tracks_not_attempted = counts, len(unique) - i - 1
            raise  # ends the run, not just this track
        except (IngestError, OSError, KeyError, ValueError, IndexError, TypeError) as e:
            detail = f": {e}" if isinstance(e, IngestError) else ""
            log(f"ERROR {label}: {type(e).__name__}{detail}")
            counts["failed"] += 1
            if limits:
                limits.attempted += 1
            if state and not dry:
                state.record(sid, "failed")
            continue
        counts[status] += 1
        if notes.get("title_only"):  # additive, only in the title-only matcher mode (#151); counts only
            counts["matched_title_only"] = counts.get("matched_title_only", 0) + 1
        if limits and status in ("new", "too_big", "dry_run"):
            limits.attempted += 1
        if state and not dry:
            if status == "no_match":
                state.record(sid, "no_match")
            elif status in ("new", "exists", "too_big"):
                state.clear(sid)
        if status == "exists":
            log(f"skipped (already present): {label}")
        elif status in ("no_match", "too_big"):
            log(f"WARN {status.replace('_', ' ')}: {label}")
        elif status in ("new", "dry_run"):
            log(f"{'ingested' if status == 'new' else 'dry-run would ingest'}: {label}"
                + (" (matched by title only)" if notes.get("title_only") else ""))
    return counts


COUNT_KEYS = ("new", "exists", "no_match", "too_big", "failed")


def summary(counts: dict) -> str:
    cooldown, title_only = counts.get("cooldown", 0), counts.get("matched_title_only", 0)
    return (f"{counts['new']} new, {counts['exists']} already present, {counts['no_match']} no match, "
            f"{counts['too_big']} too big, {counts['failed']} failed" + (f", {cooldown} in cooldown" if cooldown else "")
            + (f", {title_only} matched by title only" if title_only else ""))


def run_playlists(urls: list[str], fetch_songs: Callable[[str], list[dict]], deps: Deps, existing: set,
                  covers: dict, work: Path, dry: bool, limits: RunLimits | None = None,
                  state: RunState | None = None) -> dict[str, dict | None]:
    """Runs every playlist; one failing playlist does not hide the others. Prints one result line per playlist.
    Returns url -> counts, or None when the playlist could not be fetched (counts also hold failed tracks).
    When `limits` is exhausted the rest of the run is skipped (limits.stop_reason says why) and logged."""
    results: dict[str, dict | None] = {}
    for n, url in enumerate(urls):
        if limits and not limits.stop_reason and (reason := limits.exhausted()):
            limits.stop(reason)  # before the next playlist is even fetched
        if limits and limits.stop_reason:
            log_limit_stop(limits, len(urls) - n)
            break
        try:
            try:
                songs = fetch_songs(url)
            except (IngestError, OSError, ValueError) as e:
                if isinstance(e, YouTubeSignInRequired):
                    raise
                log(f"playlist {url}: FAILED: {type(e).__name__}: {e}")
                results[url] = None
                continue
            results[url] = run(songs, deps, existing, covers, work, dry, limits, state)
        except YouTubeSignInRequired as e:
            e.playlists_not_attempted = len(urls) - n - 1
            e.results = results
            log_interrupted(url, results, e)
            raise
        log(f"playlist {url}: {summary(results[url])}")
        if limits and limits.stop_reason:  # stopped inside this playlist
            limits.stopped_url = url
            log_limit_stop(limits, len(urls) - n - 1)
            break
    return results


def log_limit_stop(limits: RunLimits, playlists_not_started: int) -> None:
    """Counts only: why the run stopped early and how much was left."""
    what = (f"--max-new {limits.max_new} reached ({limits.attempted} attempted)" if limits.stop_reason == "cap"
            else "--max-minutes limit reached")
    log(f"stopped: {limits.stop_reason}: {what}; {limits.not_attempted} track(s) of the current playlist and "
        f"{playlists_not_started} playlist(s) not started; the next run continues")


def log_interrupted(url: str, results: dict, e: YouTubeSignInRequired) -> None:
    """Counts only, never values: what was done before the sign-in challenge and what was not attempted."""
    done = [c for c in results.values() if c is not None]
    current = e.counts or {k: 0 for k in COUNT_KEYS}
    log(f"playlist {url}: interrupted by youtube-sign-in: {summary(current)}")
    total = {k: sum(c[k] for c in [*done, current]) for k in COUNT_KEYS}
    log(f"so far: {summary(total)}; not attempted: {e.tracks_not_attempted} more track(s) of the current "
        f"playlist (plus the track that was challenged), {e.playlists_not_attempted} playlist(s) not started")


# ---- followed artists source (#121) ----
# The owner's followed artists (read scope only, no playlist is created or changed anywhere) -> each artist's tracks
# (Client Credentials catalog, ingest/artist_tracks.py) -> cross-release dedupe (ingest/dedupe.py) -> the same run()
# as a playlist, so the cap, cooldown, pacing, backoff and the sign-in stop apply unchanged. Progress lives in the
# state file: an artist scanned to the end is recorded with the day and looked at again after rescan_days.

@dataclass
class FollowedRun:
    """What the followed-artists phase did; filled while it runs, so it is complete after a sign-in stop too."""
    counts: dict = field(default_factory=lambda: {"new": 0, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0,
                                                  "failed": 0, "cooldown": 0})
    listed: int = 0             # distinct followed artists
    excluded: int = 0           # skipped: on the exclude list
    recent: int = 0             # skipped: scanned to the end less than rescan_days ago
    deferred: int = 0           # not started: max_artists reached
    scanned: int = 0            # scanned to the end in this run
    failed: int = 0             # enumeration failed (the others went on)
    not_started: int = 0        # not started: cap, time limit or sign-in stop
    not_attempted: int = 0      # tracks of the artist in progress that were not looked at
    status: str = "done"        # done | stopped | interrupted

    def add(self, counts: dict) -> None:
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v


def printable(text: str, limit: int = 80) -> str:
    """A name from an API for a log line: control characters become '?', length bounded."""
    return "".join(c if c.isprintable() else "?" for c in str(text)[:limit])


def real_enumerator(cid: str, secret: str) -> Callable[[str, tuple, str], list[dict]]:
    """artist id, groups, market -> song records. A fresh Catalog (own call budget) and token per artist."""
    import artist_tracks

    def enumerate_artist(artist: str, groups: tuple, market: str) -> list[dict]:
        cat = artist_tracks.Catalog(lambda: artist_tracks.client_credentials_token(cid, secret), [cid, secret], out=log)
        return artist_tracks.artist_tracks(cat, artist, groups, market, out=lambda _line: None)
    return enumerate_artist


def run_followed(fr: FollowedRun, cfg: FollowedConfig, artists: list[dict],
                 enumerate_artist: Callable[[str, tuple, str], list[dict]], deps: Deps, existing: set, covers: dict,
                 work: Path, dry: bool, limits: RunLimits | None, state: RunState) -> None:
    """Works through `artists` ([{"id", "name"}, ...] in the order Spotify gives them) and records in `fr`.
    Duplicate ids (paging can repeat an artist) and excluded ids are dropped, artists scanned less than
    cfg.rescan_days ago are skipped, at most cfg.max_artists are started. An artist whose enumeration fails is
    logged and counted, the others go on. A sign-in challenge ends the run (raised after `fr` is updated); a limit
    ends the phase. A scan is recorded only when it reached the last track (never in a dry run). A track that
    failed in an otherwise complete scan is retried through the cooldown of the state file, not by the artist."""
    import artist_tracks
    import dedupe as dedupe_mod
    first: dict[str, dict] = {}
    for a in artists:
        first.setdefault(a["id"], a)  # first occurrence wins, order kept
    unique = list(first.values())
    fr.listed = len(unique)
    excluded = set(cfg.exclude)
    wanted = [a for a in unique if a["id"] not in excluded]
    fr.excluded = len(unique) - len(wanted)
    due = [a for a in wanted if state.artist_due(a["id"], cfg.rescan_days)]
    fr.recent = len(wanted) - len(due)
    for n, a in enumerate(due):
        left = len(due) - n
        if cfg.max_artists is not None and n >= cfg.max_artists:
            fr.deferred = left
            log(f"stopped: max_artists {cfg.max_artists} reached; {left} artist(s) not started; the next run continues")
            return
        if limits and not limits.stop_reason and (reason := limits.exhausted()):
            limits.stop(reason)
        if limits and limits.stop_reason:
            fr.status, fr.not_started = "stopped", left
            log(f"stopped: {limits.stop_reason}: limit reached; {left} artist(s) not started; the next run continues")
            return
        label = printable(a["name"])
        try:
            songs = enumerate_artist(a["id"], cfg.groups, cfg.market)
            result = dedupe_mod.collapse(songs, collect_pairs=True)
        except (artist_tracks.ArtistTracksError, IngestError, OSError, ValueError, TypeError) as e:
            detail = f": {e}" if isinstance(e, (artist_tracks.ArtistTracksError, IngestError)) else ""
            log(f"ERROR artist {label}: enumeration failed: {type(e).__name__}{detail}")
            fr.failed += 1
            continue
        # a recording already stored under another release (its id is in `existing` and was collapsed into the kept
        # record of this scan) must not be downloaded again under the kept record's id
        present = {kept for gone, kept in result.pairs if gone in existing}
        feed = [r for r in result.kept if r["song_id"] not in present]
        log(f"artist {label}: {dedupe_mod.summary(result)}"
            + (f", {len(result.kept) - len(feed)} already stored under another release" if len(feed) != len(result.kept) else ""))
        try:
            counts = run(feed, deps, existing, covers, work, dry, limits, state)
        except YouTubeSignInRequired as e:
            fr.add(e.counts or {})
            fr.status, fr.not_started, fr.not_attempted = "interrupted", left - 1, e.tracks_not_attempted + 1
            log(f"artist {label}: interrupted by youtube-sign-in: {summary(e.counts or {k: 0 for k in COUNT_KEYS})}; "
                f"{left - 1} artist(s) not started")
            raise
        counts["exists"] += len(result.kept) - len(feed)
        fr.add(counts)
        log(f"artist {label}: {summary(counts)}")
        if limits and limits.stop_reason:  # a limit ended the artist early: not recorded, the next run resumes here
            fr.status, fr.not_started, fr.not_attempted = "stopped", left - 1, limits.not_attempted
            log_limit_stop_artists(limits, left - 1)
            return
        fr.scanned += 1
        if not dry:
            state.record_artist(a["id"])


def log_limit_stop_artists(limits: RunLimits, artists_not_started: int) -> None:
    """Counts only: why the run stopped inside an artist and how much was left."""
    what = (f"--max-new {limits.max_new} reached ({limits.attempted} attempted)" if limits.stop_reason == "cap"
            else "--max-minutes limit reached")
    log(f"stopped: {limits.stop_reason}: {what}; {limits.not_attempted} track(s) of the current artist and "
        f"{artists_not_started} artist(s) not started; the next run continues")


def followed_summary(fr: FollowedRun) -> str:
    """One line of counts for the log."""
    parts = [f"{fr.listed} followed", f"{fr.scanned} scanned", f"{fr.recent} scanned recently", f"{fr.excluded} excluded"]
    for n, what in ((fr.deferred, "deferred by max_artists"), (fr.not_started, "not started"), (fr.failed, "failed")):
        if n:
            parts.append(f"{n} {what}")
    return f"followed artists: {', '.join(parts)}; tracks: {summary(fr.counts)}"


# ---- structured result (--result-json, read by scripts/run-ingest-local.sh into last-run.json) ----
# Only counts, class names, status words and playlist URLs WITHOUT query (already validated); never an id, token or
# a message text. failure_class: none | partial (some track/playlist failed) | config (bad sources/env, nothing
# ran) | youtube-sign-in. A crash writes no file at all; the wrapper reports that as `crash`.
# Added in #119 (additive): `stop_reason` (cap | time | sign-in | done; null when nothing ran), per-playlist status
# `stopped` (a --max-new/--max-minutes limit ended the run inside it) and the count `cooldown` (tracks skipped
# because the state file says retry later).
# Added in #121 (additive, only when sources.yml has an enabled followed_artists section): `followed_artists` =
# {status: done | stopped | interrupted, the track counts of the artist phase, artists: {listed, excluded,
# skipped_recent, deferred, scanned, failed, not_started}}; its track counts are also part of `counts`.
RESULT_COUNT_KEYS = ("added", "skipped", "no_match", "too_big", "failed", "not_attempted", "cooldown")


def _result_counts(c: dict | None, not_attempted: int = 0) -> dict:
    c = c or {}
    return {"added": c.get("new", 0), "skipped": c.get("exists", 0), "no_match": c.get("no_match", 0),
            "too_big": c.get("too_big", 0), "failed": c.get("failed", 0), "not_attempted": not_attempted,
            "cooldown": c.get("cooldown", 0),
            **({"matched_title_only": c["matched_title_only"]} if c.get("matched_title_only") else {})}


def stop_reason(results: dict | None, interrupted: YouTubeSignInRequired | None, limits: RunLimits | None) -> str | None:
    """Why the run ended: sign-in, cap, time, or done (it ran through); None when it never started running."""
    if interrupted:
        return "sign-in"
    if limits and limits.stop_reason:
        return limits.stop_reason
    return "done" if results is not None else None


def build_result(urls: list[str], results: dict | None, interrupted: YouTubeSignInRequired | None,
                 exit_code: int, limits: RunLimits | None = None, followed: FollowedRun | None = None) -> dict:
    """Result dict for --result-json. `results` is url -> counts (None = unreadable playlist); with `interrupted`
    the finished playlists come from interrupted.results, the one after them is the interrupted one."""
    done = interrupted.results if interrupted else (results or {})
    playlists = []
    for n, url in enumerate(urls):
        item = {"url": url.split("?", 1)[0]}
        if url in done and done[url] is None:
            item.update(status="unreadable", **_result_counts(None))
        elif limits and limits.stopped_url == url and url in done:
            item.update(status="stopped", **_result_counts(done[url], limits.not_attempted))
        elif url in done:
            item.update(status="done", **_result_counts(done[url]))
        elif interrupted and n == len(done):
            na = interrupted.tracks_not_attempted + 1 if interrupted.counts is not None else 0
            item.update(status="interrupted", **_result_counts(interrupted.counts, na))
        else:
            item.update(status="not_attempted", **_result_counts(None))
        playlists.append(item)
    counts = {k: sum(p[k] for p in playlists) for k in RESULT_COUNT_KEYS}
    section = None
    if followed:  # additive (#121): only counts, never an artist id or name
        section = {"status": followed.status, **_result_counts(followed.counts, followed.not_attempted),
                   "artists": {"listed": followed.listed, "excluded": followed.excluded,
                               "skipped_recent": followed.recent, "deferred": followed.deferred,
                               "scanned": followed.scanned, "failed": followed.failed,
                               "not_started": followed.not_started}}
        counts = {k: counts[k] + section[k] for k in RESULT_COUNT_KEYS}
    title_only = sum(p.get("matched_title_only", 0) for p in (*playlists, *([section] if section else [])))
    if title_only:  # additive (#151): present only when the title-only matcher mode matched something
        counts["matched_title_only"] = title_only
    counts["playlists_unreadable"] = sum(p["status"] == "unreadable" for p in playlists)
    counts["playlists_not_attempted"] = sum(p["status"] == "not_attempted" for p in playlists)
    if exit_code == 0:
        cls = "none"
    elif exit_code == EXIT_YOUTUBE_SIGN_IN:
        cls = FAILURE_YOUTUBE_SIGN_IN
    elif results is not None:
        cls = "partial"
    else:
        cls = "config"
    result = {"failure_class": cls, "exit_code": exit_code, "stop_reason": stop_reason(results, interrupted, limits),
              "playlists": playlists, "counts": counts}
    if section:
        result["followed_artists"] = section
    return result


def write_result(path: Path, result: dict) -> None:
    """Atomic (temp file + rename in the same directory), mode 0600. A write failure is logged, never silent, and
    does not change the exit code."""
    try:
        atomic_write_json(path, result)
    except OSError as e:
        log(f"WARN could not write the result file: {type(e).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync public Spotify playlists into the library.")
    ap.add_argument("playlist_url", nargs="?", help="one playlist URL (or use --sources)")
    ap.add_argument("--sources", type=Path, metavar="FILE", help="sources.yml: sync every playlist listed in it")
    ap.add_argument("--dry-run", action="store_true", help="match only; download and upload nothing")
    ap.add_argument("--result-json", type=Path, metavar="PATH",
                    help="write the structured result (counts, failure class) to PATH at the end of the run")
    ap.add_argument("--max-new", type=positive_int, metavar="N", default=None,
                    help="stop after N tracks were attempted in this run (new, too big, failed; not skipped, "
                         "cooldown or no-match tracks). Default: no cap")
    ap.add_argument("--max-minutes", type=positive_float, metavar="M", default=None,
                    help="stop starting new tracks after M minutes of wall clock. Default: no time limit")
    ap.add_argument("--state-file", type=Path, metavar="PATH", default=None,
                    help="local state (cooldown of no_match/failed tracks). Default: "
                         "${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/ingest-state.json")
    ap.add_argument("--retry-no-match-days", type=non_negative_int, metavar="D", default=DEFAULT_RETRY_NO_MATCH_DAYS,
                    help=f"do not retry a no_match track for D days (0 = always retry; default {DEFAULT_RETRY_NO_MATCH_DAYS})")
    ap.add_argument("--retry-failed-days", type=non_negative_int, metavar="D", default=DEFAULT_RETRY_FAILED_DAYS,
                    help=f"do not retry a failed track for D days (0 = always retry; default {DEFAULT_RETRY_FAILED_DAYS})")
    ap.add_argument("--pace-seconds", type=non_negative_float, metavar="S", default=DEFAULT_PACE_SECONDS,
                    help=f"pause at least S seconds between downloads (0 = none; default {DEFAULT_PACE_SECONDS:g})")
    ap.add_argument("--pace-jitter-seconds", type=non_negative_float, metavar="J", default=DEFAULT_PACE_JITTER_SECONDS,
                    help="add a random 0..J seconds to every pause (0 = fixed pause; default "
                         f"{DEFAULT_PACE_JITTER_SECONDS:g}, so 5-10 s together with --pace-seconds)")
    ap.add_argument("--retry-attempts", type=positive_int, metavar="N", default=DEFAULT_ATTEMPTS,
                    help="attempts in total for a transient error (HTTP 429/5xx, network) with exponential backoff "
                         f"(1 = no retry; default {DEFAULT_ATTEMPTS}). The YouTube sign-in challenge is never retried")
    args = ap.parse_args()
    if bool(args.playlist_url) == bool(args.sources):
        ap.error("give exactly one of PLAYLIST_URL or --sources FILE")
    state: dict = {"urls": [], "results": None, "interrupted": None, "limits": None, "followed": None}
    code = execute(args, state)
    if args.result_json:
        write_result(args.result_json, build_result(state["urls"], state["results"], state["interrupted"], code,
                                                    state["limits"], state["followed"]))
    return code


def _bounded(text: str, kind: type, low: float, high: float):
    try:
        v = kind(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text[:20]!r}") from None
    if not low <= v <= high:  # also rejects nan and inf
        raise argparse.ArgumentTypeError(f"must be between {low:g} and {high:g}")
    return v


def positive_int(text: str) -> int:
    return _bounded(text, int, 1, 10**9)


def non_negative_int(text: str) -> int:
    return _bounded(text, int, 0, 36500)


def non_negative_float(text: str) -> float:
    return _bounded(text, float, 0, 3600)


def positive_float(text: str) -> float:
    v = _bounded(text, float, 0, 10**6)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return v


def execute(args: argparse.Namespace, state: dict) -> int:
    """The run itself. Records what the structured result needs in `state` (urls, results, interrupted)."""
    try:
        if args.sources:
            config = parse_source_config(args.sources.read_text(encoding="utf-8-sig"))
        else:
            config = SourceConfig([validate_playlist_url(args.playlist_url)], None)
        urls = config.urls
    except (IngestError, OSError, UnicodeDecodeError) as e:
        log(f"ERROR {e}")
        return 1
    state["urls"] = urls
    try:
        tg_check.load_env()
    except tg_check.EnvSyntaxError as e:
        log(f"ERROR {e}")
        return 1
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        log(f"ERROR missing env vars: {', '.join(missing)}")
        return 1

    problem = js_runtime_problem()
    if problem:
        log(f"WARN js runtime: {problem}; yt-dlp needs Deno >= {'.'.join(map(str, MIN_DENO))} for YouTube")

    followed_cfg = config.followed if config.followed and config.followed.enabled else None
    followed_artists_list: list[dict] = []
    if followed_cfg:  # before any work: a missing or invalid user login is a config error, nothing runs
        import spotify_user
        if not os.environ.get(spotify_user.REFRESH_KEY):
            log(f"ERROR followed_artists is enabled in {args.sources} but {spotify_user.REFRESH_KEY} is not set in "
                f".env: run `python ingest/spotify_user.py login` once (docs/local-ingest.md), or set "
                f"`enabled: false` in the followed_artists section; exit code {EXIT_CONFIG}")
            return EXIT_CONFIG
        try:
            followed_artists_list = spotify_user.followed_artists(spotify_user.get_access_token())
        except (spotify_user.UserAuthError, tg_check.EnvSyntaxError) as e:  # redacted, never holds a token
            log(f"ERROR cannot read the followed artists: {e}; exit code {EXIT_CONFIG}")
            return EXIT_CONFIG
        log(f"followed artists: {len(followed_artists_list)} listed by Spotify")

    from ytmusicapi import YTMusic
    existing = {r["spotify_id"] for r in d1_query("SELECT spotify_id FROM tracks")}
    covers = {r["id"]: r["cover_file_id"] for r in d1_query("SELECT id, cover_file_id FROM albums")}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        throttle = Throttle(args.pace_seconds, args.pace_jitter_seconds, args.retry_attempts)

        def fetch_songs(url: str) -> list[dict]:
            save = work / "list.spotdl"
            save.unlink(missing_ok=True)
            throttle.call(lambda: spotdl_run(["save", url, "--save-file", str(save)], work), transient_text,
                          "Spotify playlist fetch")
            return json.loads(save.read_text(encoding="utf-8"))

        limits = RunLimits(args.max_new, args.max_minutes)
        state["limits"] = limits
        track_state = RunState.load(args.state_file or default_state_path(),
                                    {"no_match": args.retry_no_match_days, "failed": args.retry_failed_days})
        try:
            deps = real_deps(YTMusic(), throttle)
            results = run_playlists(urls, fetch_songs, deps, existing, covers, work, args.dry_run, limits, track_state)
            if followed_cfg:
                fr = state["followed"] = FollowedRun()
                try:
                    run_followed(fr, followed_cfg, followed_artists_list,
                                 real_enumerator(os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]),
                                 deps, existing, covers, work, args.dry_run, limits, track_state)
                except YouTubeSignInRequired as e:
                    e.results, e.playlists_not_attempted = results, 0  # every playlist was finished before
                    raise
        except YouTubeSignInRequired as e:
            state["interrupted"] = e
            log(f"ERROR {FAILURE_YOUTUBE_SIGN_IN}: YouTube asks to sign in (bot check); skipping the rest of the "
                f"run (remaining tracks and playlists), not retrying; manual action needed: wait and re-run later, "
                f"do not add cookies; exit code {EXIT_YOUTUBE_SIGN_IN}")
            log(f"stop reason: {stop_reason({}, e, limits)}")
            return EXIT_YOUTUBE_SIGN_IN
    state["results"] = results
    ok = [c for c in results.values() if c is not None]
    fr = state["followed"]
    total = {k: sum(c.get(k, 0) for c in [*ok, *([fr.counts] if fr else [])]) for k in (*COUNT_KEYS, "cooldown", "matched_title_only")}
    broken = len(results) - len(ok)
    if fr:
        log(followed_summary(fr))
    log(f"done: {summary(total)}" + (f"; {broken} playlist(s) could not be read" if broken else "")
        + (f"; {fr.failed} artist(s) could not be enumerated" if fr and fr.failed else ""))
    log(f"stop reason: {stop_reason(results, None, limits)}")
    return 1 if broken or total["failed"] or (fr and fr.failed) else 0


if __name__ == "__main__":
    sys.exit(main())

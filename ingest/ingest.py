"""Sync public Spotify playlists into the library: match -> download -> Telegram -> D1.

Usage: python ingest/ingest.py (PLAYLIST_URL | --sources sources.yml) [--dry-run] [--result-json PATH]
                               [--max-new N] [--max-minutes M] [--state-file PATH]
                               [--retry-no-match-days D] [--retry-failed-days D]
                               [--pace-seconds S] [--pace-jitter-seconds J] [--retry-attempts N]
One playlist failing does not stop the others; the exit code is 1 if any playlist or track failed.
sources.yml may also have a `followed_artists:` section (#121): the tracks of every artist the owner follows on
Spotify are ingested too, no playlist is created. Needs SPOTIFY_USER_REFRESH_TOKEN (python ingest/spotify_user.py
login); without a usable login the run ends with exit code 2 before doing any work.
sources.yml may also have a `lastfm_artists:` section (#152): the tracks of your top Last.fm artists of the last years are
ingested too. Needs LASTFM_API_KEY in the environment; without a usable key or profile the run ends with exit code 2.
sources.yml may also have a `new_releases:` section (#172): after everything else a bounded check of the artists of the
stored bootstrap ranking for releases that are not in the library (docs/local-ingest.md). Off unless present.
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
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tg_check
from match import MODE_TITLE_ONLY, cover_url_from_match, find_match, normalize

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
    REASON_MAX characters. Empty when the body carries no string reason. A secret split by control or zero-width
    characters is caught by a second redaction on the text with those characters dropped (redact, clean, redact)."""
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    if not isinstance(msg, str):
        return ""
    first = redact(msg, secrets)
    bare = "".join(c for c in first if c.isprintable())
    again = redact(bare, secrets)
    if again != bare:  # a secret was hiding behind control characters: use the redacted, control-free text
        text = again.strip()
    else:
        text = "".join(c if c.isprintable() else "?" for c in first).strip()
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
class LastfmConfig:
    """The `lastfm_artists:` section of sources.yml (#152). The API key is never a key here: env LASTFM_API_KEY only."""
    enabled: bool = True
    user: str = ""                      # Last.fm user name; required when enabled
    years: int = 5                      # listening window of the ranking, in years
    max_artists: int = 100              # size of the ranking (the top N artists by playcount)
    rescan_days: int = 7                # a fully scanned artist is looked at again after this many days (0 = every run)
    exclude: tuple[str, ...] = ()       # artist names, compared by match.normalize


@dataclass(frozen=True)
class NewReleasesConfig:
    """The `new_releases:` section of sources.yml (#172): the nightly check for missing releases of the artists of the
    stored bootstrap ranking. Off unless the section is present and enabled; the defaults are the caps accepted in #165."""
    enabled: bool = True
    max_artists: int = 10               # due artists checked per night
    recheck_days: int = 7               # a checked artist is due again after this many days
    max_new: int = 20                   # tracks added by this phase per night


@dataclass(frozen=True)
class SourceConfig:
    urls: list[str]
    followed: FollowedConfig | None     # None: no section (a plain playlist file)
    lastfm: LastfmConfig | None = None  # None: no lastfm_artists section
    new_releases: NewReleasesConfig | None = None  # None: no new_releases section


FOLLOWED_HEADER = "followed_artists:"
FOLLOWED_KEY = re.compile(r"  ([a-z_]+):(?: (\S.*))?")  # exactly two spaces of indent
FOLLOWED_ITEM = re.compile(r"    - (\S+)")              # exactly four spaces, under `exclude:`
FOLLOWED_KEYS = ("enabled", "max_artists", "rescan_days", "groups", "market", "exclude")
ARTIST_ID = re.compile(r"[A-Za-z0-9]{22}")
LASTFM_ARTIST_KEY = re.compile(r"lfm:[0-9a-f]{16}")   # state key of a Last.fm artist (#152), see lastfm_artist_key
FOLLOWED_GROUPS = ("album", "single", "compilation", "appears_on")  # ingest/artist_tracks.py ALL_GROUPS (tested)
INT_VALUE = re.compile(r"[0-9]{1,5}")


LASTFM_HEADER = "lastfm_artists:"
LASTFM_ITEM = re.compile(r"    - (\S.*)")                # exactly four spaces, under `exclude:`; the rest is the name
LASTFM_KEYS = ("enabled", "user", "years", "max_artists", "rescan_days", "exclude")
LASTFM_USER = re.compile(r"[A-Za-z0-9_-]{2,15}")        # same as lastfm._USER (tested): the client is not imported here
LASTFM_EXCLUDE_MAX = 500                                # names in the exclude list
LASTFM_NAME_MAX = 200                                   # characters of one excluded name (lastfm.NAME_MAX, tested)


def _lastfm_value(key: str, value: str):
    """Typed value of one `key: value` line of the lastfm_artists section; rejects, never rewrites."""
    if key == "enabled":
        return _followed_value(key, value)
    if key == "user":
        if not LASTFM_USER.fullmatch(value):
            raise ValueError("user must be a Last.fm user name: 2-15 letters, digits, _ or - (not a URL)")
        return value
    low, high = {"years": (1, 10), "max_artists": (1, 10000), "rescan_days": (0, 3650)}[key]
    if not INT_VALUE.fullmatch(value) or not low <= int(value) <= high:
        raise ValueError(f"{key} must be a whole number from {low} to {high}")
    return int(value)


NEW_RELEASES_HEADER = "new_releases:"
NEW_RELEASES_KEYS = ("enabled", "max_artists", "recheck_days", "max_new")
NEW_RELEASES_RANGES = {"max_artists": (1, 1000), "recheck_days": (1, 365), "max_new": (1, 1000)}


def _new_releases_value(key: str, value: str):
    """Typed value of one `key: value` line of the new_releases section; rejects, never rewrites."""
    if key == "enabled":
        return _followed_value(key, value)
    low, high = NEW_RELEASES_RANGES[key]
    if not INT_VALUE.fullmatch(value) or not low <= int(value) <= high:
        raise ValueError(f"{key} must be a whole number from {low} to {high}")
    return int(value)


def _lastfm_exclude_item(name: str, seen: list[str]) -> str:
    """One excluded artist name: printable, bounded, not quoted, no inline comment, not a repeat (after normalising)."""
    if len(name) > LASTFM_NAME_MAX or not name.isprintable():
        raise ValueError(f"an excluded name must be printable and at most {LASTFM_NAME_MAX} characters")
    if name[0] in "\"'" or re.search(r"\s#", name):
        raise ValueError("an excluded name is written as is: no quotes and no inline comment")
    norm = normalize(name)
    if not norm:
        raise ValueError("an excluded name has nothing left after normalising")
    if len(seen) >= LASTFM_EXCLUDE_MAX:
        raise ValueError(f"at most {LASTFM_EXCLUDE_MAX} excluded names")
    if norm in (normalize(x) for x in seen):
        raise ValueError("artist listed twice")
    return name


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
    tolerated outside the sections); blank lines and whole-line `#` comments are ignored, anything else is an error
    (no silent skip). Every URL passes validate_playlist_url. Optional sections, each at most once, header at column 0:

        followed_artists:
          enabled: true          (two-space indent; keys: enabled max_artists rescan_days groups market exclude)
          max_artists: 20
          exclude:
            - <22-char artist id>    (four-space indent)

        lastfm_artists:          (#152; keys: enabled user years max_artists rescan_days exclude)
          user: some_name
          years: 5
          exclude:
            - <artist name, as is>

        new_releases:            (#172; keys: enabled max_artists recheck_days max_new; no list)
          max_artists: 10

    A section ends at the next line without indent. Unknown or repeated keys, a malformed value, a repeated
    excluded entry and anything else is an error. A file without a section behaves exactly as before."""
    urls: list[str] = []
    cur: str | None = None  # name of the section being read
    seen: dict[str, bool] = {"followed_artists": False, "lastfm_artists": False, "new_releases": False}
    fields: dict[str, dict] = {"followed_artists": {}, "lastfm_artists": {}, "new_releases": {}}
    exclude: dict[str, list[str]] = {"followed_artists": [], "lastfm_artists": []}
    keys = {"followed_artists": FOLLOWED_KEYS, "lastfm_artists": LASTFM_KEYS, "new_releases": NEW_RELEASES_KEYS}
    headers = {FOLLOWED_HEADER: "followed_artists", LASTFM_HEADER: "lastfm_artists",
               NEW_RELEASES_HEADER: "new_releases"}
    in_exclude = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if cur and line[0] in " \t":
            where = f"sources line {no} ({cur})"
            item_rx = FOLLOWED_ITEM if cur in ("followed_artists", "new_releases") else LASTFM_ITEM
            if m := item_rx.fullmatch(line):
                if cur == "new_releases":
                    raise IngestError(f"{where}: this section takes no list")
                if not in_exclude:
                    raise IngestError(f"{where}: a list item is only allowed under 'exclude:'")
                if cur == "followed_artists":
                    if not ARTIST_ID.fullmatch(m[1]):
                        raise IngestError(f"{where}: not a Spotify artist id (22 letters/digits): {m[1][:40]!r}")
                    if m[1] in exclude[cur]:
                        raise IngestError(f"{where}: artist id listed twice")
                else:
                    try:
                        _lastfm_exclude_item(m[1], exclude[cur])
                    except ValueError as e:
                        raise IngestError(f"{where}: {e}") from None
                exclude[cur].append(m[1])
                continue
            m = FOLLOWED_KEY.fullmatch(line)
            if not m:
                if cur == "new_releases":
                    raise IngestError(f"{where}: expected '  <key>: <value>' (two spaces)")
                what = "artist id" if cur == "followed_artists" else "artist name"
                raise IngestError(f"{where}: expected '  <key>: <value>' (two spaces) or '    - <{what}>'")
            key, value = m[1], m[2]
            if key not in keys[cur]:
                raise IngestError(f"{where}: unknown key {key!r} (allowed: {', '.join(keys[cur])})")
            if key in fields[cur]:
                raise IngestError(f"{where}: key {key!r} given twice")
            in_exclude = key == "exclude"
            if in_exclude:
                what = "artist id" if cur == "followed_artists" else "artist name"
                if value is not None:
                    raise IngestError(f"{where}: 'exclude:' takes a list on the following lines ('    - <{what}>')")
                fields[cur][key] = ()
            elif value is None:
                raise IngestError(f"{where}: key {key!r} has no value")
            else:
                try:
                    parse_value = {"followed_artists": _followed_value, "lastfm_artists": _lastfm_value,
                                   "new_releases": _new_releases_value}[cur]
                    fields[cur][key] = parse_value(key, value)
                except ValueError as e:
                    raise IngestError(f"{where}: {e}") from None
            continue
        cur, in_exclude = None, False
        if line in headers:
            name = headers[line]
            if seen[name]:
                raise IngestError(f"sources line {no}: section '{name}' given twice")
            seen[name] = True
            cur = name
            continue
        m = SOURCES_ITEM.fullmatch(line.strip())
        if not m:
            raise IngestError(f"sources line {no}: expected '- <playlist url>'")
        try:
            urls.append(validate_playlist_url(m[1]))
        except IngestError as e:
            raise IngestError(f"sources line {no}: {e}") from None
    followed = lastfm = new_releases = None
    if seen["new_releases"]:  # not a source of tracks to ingest: it does not count for the "no playlists" check below
        new_releases = NewReleasesConfig(**fields["new_releases"])
    if seen["followed_artists"]:
        fields["followed_artists"]["exclude"] = tuple(exclude["followed_artists"])
        followed = FollowedConfig(**fields["followed_artists"])
    if seen["lastfm_artists"]:
        fields["lastfm_artists"]["exclude"] = tuple(exclude["lastfm_artists"])
        lastfm = LastfmConfig(**fields["lastfm_artists"])
        if lastfm.enabled and not lastfm.user:
            raise IngestError("sources: section 'lastfm_artists' needs 'user' (your Last.fm user name) when enabled")
    if not urls and not (followed and followed.enabled) and not (lastfm and lastfm.enabled):
        present = [n for n, c in (("followed_artists", followed), ("lastfm_artists", lastfm)) if c]
        raise IngestError("sources file lists no playlists" + (f" and {' and '.join(present)} "
                          f"{'is' if len(present) == 1 else 'are'} not enabled" if present else ""))
    return SourceConfig(list(dict.fromkeys(urls)), followed, lastfm, new_releases)


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
                     "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID", "SPOTIFY_USER_REFRESH_TOKEN", "LASTFM_API_KEY",
                     "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"}  # the last two go through argv
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
    # Fail closed: nothing that load_env took from .env reaches the child (whatever the key is called); the outer
    # environment passes. SPOTDL_HIDDEN_ENV is a second line of defence for the same names set by the caller.
    env = {k: v for k, v in os.environ.items() if k not in tg_check.ENV_FROM_FILE and k not in SPOTDL_HIDDEN_ENV}
    env["COLUMNS"] = SPOTDL_CONSOLE_COLUMNS
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    lines = clean_lines(r.stdout + "\n" + r.stderr, [cid, secret])
    tail = "\n".join(lines[-TAIL_LINES:])
    if r.returncode != 0:
        if is_sign_in_challenge(cause_lines(lines) + lines[-TAIL_LINES:]):
            raise YouTubeSignInRequired("YouTube asks to sign in (bot check)")
        raise IngestError(f"spotdl {args[0]} failed with exit code {r.returncode}: {tail}")
    return cause_lines(lines), tail


# ---- direct yt-dlp download for songs without a Spotify URL (Last.fm, #240) ----
# spotdl's "<youtube url>|<spotify url>" form needs a real open.spotify.com track URL; a url-less song would make it
# fetch metadata for a fake id and fail. Such songs skip spotdl: yt-dlp downloads the matched video and WE write the
# ID3 tags. Same bot-check rules as everywhere (#62): no cookies, proxies, PO tokens or client rotation; yt-dlp runs
# with --ignore-config so no config file of the user's can add any of them.
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]  # tests replace this with a fake
YTDLP_TIMEOUT_S = 600  # one download; a hung yt-dlp is killed and the track fails
YTDLP_AUDIO_FILE = "audio"  # output name stem inside the track's temp dir
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TAG_TEXT_MAX = 300
TAG_NUMBER_MAX = 9999


def ytdlp_args(video_id: str, tdir: Path) -> list[str]:
    if not VIDEO_ID.fullmatch(video_id or ""):
        raise IngestError("invalid YouTube video id")
    return ["--ignore-config", "--no-playlist", "--no-progress", "--format", "bestaudio/best", "--extract-audio",
            "--audio-format", "mp3", "--audio-quality", BITRATE, "--output", str(tdir / f"{YTDLP_AUDIO_FILE}.%(ext)s"),
            "--", f"https://music.youtube.com/watch?v={video_id}"]


def ytdlp_run(video_id: str, tdir: Path) -> tuple[list[str], str]:
    """Runs yt-dlp for one video; returns (cause lines, redacted tail <= TAIL_LINES) like spotdl_run. Unlike spotdl,
    yt-dlp exits non-zero when the download failed. The sign-in challenge raises YouTubeSignInRequired."""
    cmd = [*YTDLP_CMD, *ytdlp_args(video_id, tdir)]
    env = {k: v for k, v in os.environ.items() if k not in tg_check.ENV_FROM_FILE and k not in SPOTDL_HIDDEN_ENV}
    # own process group: a hung yt-dlp is killed together with the ffmpeg it started
    proc = subprocess.Popen(cmd, cwd=tdir.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=YTDLP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise IngestError(f"yt-dlp timed out after {YTDLP_TIMEOUT_S} s") from None
    lines = clean_lines(stdout + "\n" + stderr, [])
    tail = "\n".join(lines[-TAIL_LINES:])
    causes = [*cause_lines(lines), *[ln for ln in lines if ln.startswith("ERROR:")][-MAX_CAUSES:]]
    causes = list(dict.fromkeys(causes))[-MAX_CAUSES:]
    failed = proc.returncode != 0 or not any(tdir.glob("*.mp3"))  # a bot-check WARNING beside a good mp3 is no stop
    if failed and is_sign_in_challenge(lines):
        raise YouTubeSignInRequired("YouTube asks to sign in (bot check)")
    if proc.returncode != 0:
        raise IngestError(f"yt-dlp failed with exit code {proc.returncode}"
                          + (f"; yt-dlp error: {' | '.join(causes)}" if causes else "")
                          + (f"; yt-dlp output (last lines):\n{tail}" if tail else ""))
    return causes, tail


def tag_text(value) -> str | None:
    """A tag value from ingest data: printable text only (control and format characters dropped, whitespace
    collapsed), at most TAG_TEXT_MAX characters; None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(c for c in value if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co", "Cn")).split())
    return text[:TAG_TEXT_MAX] or None


def tag_number(value) -> str | None:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= TAG_NUMBER_MAX else None


def write_id3_tags(path: Path, song: dict) -> None:
    """ID3v2.3 tags the way spotdl writes them for playlist tracks (title, artists, album, album artist, track, disc,
    year); mutagen (a dependency of spotdl, already installed) writes the file directly: no shell, no ffmpeg
    arguments. Replaces whatever tags yt-dlp/ffmpeg left in the file (the YouTube video title, for one)."""
    try:
        from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TPOS, TRCK, TYER
        from mutagen import MutagenError
    except ImportError:  # not caught per track by run(): fail this track, not the session
        raise IngestError("the mutagen package is not installed (ImportError)") from None
    artists = [a for a in (tag_text(x) for x in song.get("artists") or []) if a]
    year = tag_number(song.get("year"))
    frames = [(TIT2, tag_text(song.get("name"))), (TPE1, artists or None), (TALB, tag_text(song.get("album_name"))),
              (TPE2, tag_text(song.get("album_artist"))), (TRCK, tag_number(song.get("track_number"))),
              (TPOS, tag_number(song.get("disc_number"))), (TYER, year)]
    tags = ID3()
    for frame, value in frames:
        if value:
            tags.add(frame(encoding=3, text=value))
    try:
        tags.save(str(path), v2_version=3)
    except (MutagenError, OSError) as e:
        raise IngestError(f"could not write the ID3 tags ({type(e).__name__})") from e


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


# Hosts of the thumbnail URLs the YouTube Music search returns for songs (observed 2026-10-11, see docs/cover-art.md).
COVER_FROM_MATCH = "cover_from_match"  # song key set by process_track: the cover URL came from the YouTube Music match
YTM_COVER_HOSTS = frozenset({"lh3.googleusercontent.com", "yt3.googleusercontent.com"})
COVER_TOTAL_TIMEOUT_S = 60  # wall clock for one cover download: the socket timeout alone lets a slow drip run forever
COVER_CHUNK = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A cover URL comes from third-party search output: a redirect could end on any host (or on http), so none is
    followed; urllib then raises HTTPError for the 3xx answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cover_opener(*extra_handlers) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect, *extra_handlers)


def _fetch_cover_checked(url: str, allowed_hosts: frozenset, opener, clock) -> bytes:
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https":
        raise IngestError("cover url must be https")
    try:
        port = parts.port
    except ValueError:
        raise IngestError("cover url has an invalid port") from None
    if parts.username or parts.password or port not in (None, 443) or (parts.hostname or "") not in allowed_hosts:
        raise IngestError("cover host is not allowed")
    deadline = clock() + COVER_TOTAL_TIMEOUT_S
    with opener.open(url, timeout=30) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "image/jpeg":
            raise IngestError("cover is not image/jpeg")
        chunks, total = [], 0
        while True:
            if clock() > deadline:
                raise IngestError("cover download too slow")
            chunk = r.read(COVER_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > COVER_MAX_BYTES:
                raise IngestError("cover too large")
    data = b"".join(chunks)
    if not data.startswith(b"\xff\xd8"):
        raise IngestError("cover is not a JPEG")
    return data


def fetch_cover(url: str, dest: Path, allowed_hosts: frozenset | None = None, opener=None, clock=time.monotonic) -> Path:
    """Spotify URLs (allowed_hosts None) keep the original behaviour. With allowed_hosts (the YouTube Music route, #248):
    https, an allowlisted host, no redirect, Content-Type image/jpeg, JPEG magic bytes, the size cap and a total deadline."""
    if allowed_hosts is not None:
        dest.write_bytes(_fetch_cover_checked(url, allowed_hosts, opener or _cover_opener(), clock))
        return dest
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
    if getattr(m, "relaxed", False) is True and notes is not None:  # #194: only the relaxed title pass accepted it
        notes["relaxed"] = True
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
        if not cover and not song.get("cover_url"):  # no Spotify cover (Last.fm, new releases): the match may have one
            album_name = song.get("album_name")
            url, why = cover_url_from_match(None if album_name == UNKNOWN_ALBUM else album_name, primary_artist(song), m)
            if url:
                song = {**song, "cover_url": url, COVER_FROM_MATCH: True}
                if notes is not None:
                    notes["cover_from_match"] = True
            elif why != "no_thumbnail":  # a match without a thumbnail is the old behaviour: silent
                log(f"cover skipped for {sid}: {why}")
                if notes is not None:
                    notes["cover_skipped"] = True
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


STORED_ALBUMS_SQL = ("SELECT ar.name AS artist, al.name AS album, al.year AS year FROM albums al "
                     "JOIN artists ar ON ar.id = al.artist_id")


def read_stored_albums(query: Callable[[str], list[dict]] = d1_query) -> dict[str, list[tuple]]:
    """normalize(artist) -> [(album name, year or None)] of every stored album (#172): one read-only parameterless
    SELECT, once per run; the per-artist filtering is done here in Python, never by string-built SQL."""
    out: dict[str, list[tuple]] = {}
    for r in query(STORED_ALBUMS_SQL):
        if isinstance(r["artist"], str) and isinstance(r["album"], str):
            out.setdefault(normalize(r["artist"]), []).append((r["album"], r["year"]))
    return out


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
        if not song.get("url"):  # no Spotify URL (Last.fm, #240): spotdl cannot take it, yt-dlp does
            causes, tail = ytdlp_run(m.video_id, tdir)
            files = list(tdir.glob("*.mp3"))
            if len(files) != 1:
                raise IngestError("expected exactly one mp3 after download"
                                  + (f"; yt-dlp error: {' | '.join(causes)}" if causes else "")
                                  + (f"; yt-dlp output (last lines):\n{tail}" if tail else ""))
            write_id3_tags(files[0], song)
            return files[0]
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
        photo = fetch_cover(song["cover_url"], tdir / "cover.jpg",
                            YTM_COVER_HOSTS if song.get(COVER_FROM_MATCH) else None)
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
                if not (ARTIST_ID.fullmatch(aid) or LASTFM_ARTIST_KEY.fullmatch(aid)):
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


class PhaseLimits(RunLimits):
    """The limits of one phase that has caps of its own (#172: at most max_new tracks and 20 minutes) on top of the
    run's: exhausted() is true when the run's limits or the phase's are. Every attempt counts for both. A stop caused by
    the run's own limit is passed on to the run, so its stop reason is the same as if the phase were not there."""

    def __init__(self, parent: RunLimits, max_new: int, max_minutes: float):
        self.parent, self._attempted = parent, 0
        super().__init__(max_new, max_minutes, parent.clock)

    @property
    def attempted(self) -> int:
        return self._attempted

    @attempted.setter
    def attempted(self, value: int) -> None:
        self.parent.attempted += value - self._attempted
        self._attempted = value

    def exhausted(self) -> str | None:
        return self.parent.exhausted() or super().exhausted()

    def stop(self, reason: str, not_attempted: int = 0) -> None:
        super().stop(reason, not_attempted)
        if own := self.parent.exhausted():
            self.parent.stop(own, not_attempted)


def run(songs: list[dict], deps: Deps, existing: set, covers: dict, work: Path, dry: bool,
        limits: RunLimits | None = None, state: RunState | None = None, outcomes: dict | None = None,
        quiet: bool = False) -> dict:
    """`outcomes` (optional, #172) receives sid -> new | exists | no_match | too_big | dry_run | failed | cooldown for
    every track that was looked at. `quiet` (optional, #172) is for a feed whose log must hold no artist or title: a
    track is named by its song id and an error by its class only."""
    counts = {"new": 0, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0, "failed": 0, "cooldown": 0}
    unique = dedupe(songs)
    for i, song in enumerate(unique):
        label = f"{(song.get('artists') or ['?'])[0]} - {song.get('name', '?')}"
        sid = song.get("song_id") or ""
        if quiet:
            label = sid
        if sid not in existing:
            if state and state.in_cooldown(sid):  # does not use the cap
                counts["cooldown"] += 1
                if outcomes is not None:
                    outcomes[sid] = "no_match" if state.entries[sid]["status"] == "no_match" else "cooldown"
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
            detail = f": {e}" if isinstance(e, IngestError) and not quiet else ""
            log(f"ERROR {label}: {type(e).__name__}{detail}")
            counts["failed"] += 1
            if outcomes is not None:
                outcomes[sid] = "failed"
            if limits:
                limits.attempted += 1
            if state and not dry:
                state.record(sid, "failed")
            continue
        counts[status] += 1
        if outcomes is not None:
            outcomes[sid] = status
        if notes.get("title_only"):  # additive, only in the title-only matcher mode (#151); counts only
            counts["matched_title_only"] = counts.get("matched_title_only", 0) + 1
        if notes.get("relaxed"):  # additive (#194): counts only
            counts["matched_normalized"] = counts.get("matched_normalized", 0) + 1
        for note in ("cover_from_match", "cover_skipped"):  # additive (#248): counts only, present when non-zero
            if notes.get(note):
                counts[note] = counts.get(note, 0) + 1
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
                + (" (matched by title only)" if notes.get("title_only") else "")
                + (" (matched after title normalization)" if notes.get("relaxed") else ""))
    return counts


COUNT_KEYS = ("new", "exists", "no_match", "too_big", "failed")


def summary(counts: dict) -> str:
    cooldown, title_only = counts.get("cooldown", 0), counts.get("matched_title_only", 0)
    normalized = counts.get("matched_normalized", 0)
    return (f"{counts['new']} new, {counts['exists']} already present, {counts['no_match']} no match, "
            f"{counts['too_big']} too big, {counts['failed']} failed" + (f", {cooldown} in cooldown" if cooldown else "")
            + (f", {title_only} matched by title only" if title_only else "")
            + (f", {normalized} matched after title normalization" if normalized else "")
            + (f", {counts['cover_from_match']} covers from the match" if counts.get("cover_from_match") else "")
            + (f", {counts['cover_skipped']} without a cover" if counts.get("cover_skipped") else ""))


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


# ---- Last.fm artists source (#152) ----
# The owner's top artists over the last N years (ingest/lastfm.py, fetched at the start of every run: nothing is cached,
# Last.fm ToS cl. 4.3 [R] ties caching to the response headers, which the client does not read) -> each artist's tracks
# (ingest/lastfm_tracks.py) -> cross-release dedupe -> drop what is already stored under another id (Spotify rows) ->
# the same run() as a playlist, with the title-only matcher mode for tracks without a duration. The cap, cooldown,
# pacing and the sign-in stop apply unchanged; progress lives in the `artists` map of the state file, keyed by
# lastfm_artist_key(name). Imported lazily by execute(): a run without this section never loads ingest/lastfm*.py.

@dataclass
class LastfmRun(FollowedRun):
    """What the Last.fm phase did (the counters of FollowedRun; `deferred` stays 0: max_artists sizes the ranking)."""
    dropped_stored: int = 0     # records dropped because the same track is already stored under another id
    unusable: int = 0           # ranked artists skipped: nothing left of the name after normalising
    config_error: bool = False  # the ranking was refused for a reason only the owner can fix (exit 2 after the playlists)


def lastfm_artist_key(name: str) -> str:
    """Stable state key of a Last.fm artist: derived from the normalised name, accepted by RunState."""
    return LASTFM_ID_PREFIX + stable_id("lastfm artist", normalize(name))


LASTFM_ARTIST_CALLS = 150  # lastfm_tracks.CALL_BUDGET (requests per artist); a test keeps the two equal


def real_lastfm_enumerator(client, full: bool = False) -> Callable[[dict], "list[dict] | object"]:
    """ranked artist ({name, mbid, playcount}) -> song records. One client for the run: its pacing is continuous and
    each artist has its own request budget (lastfm_tracks.CALL_BUDGET). With full=True (#166, the bootstrap) it
    returns the whole lastfm_tracks.ArtistTracks instead, so run_lastfm can hand its `status` (ok | unknown_artist |
    no_albums | unusable_artist) and `albums_truncated` to the per-artist hook; an unknown artist or one without
    albums is a status, not an exception."""
    import lastfm_tracks

    def enumerate_artist(artist: dict):
        res = lastfm_tracks.artist_tracks(client, artist["name"], artist.get("mbid"), out=log)
        return res if full else res.records
    return enumerate_artist


@dataclass
class ArtistReport:
    """What the per-artist hook of run_lastfm (#166) receives after each artist. Counts and words only, no names."""
    key: str                  # lastfm_artist_key(name), the key of the engine state
    outcome: str              # done | cut | failed
    enum: str | None          # ok | unknown_artist | no_albums | unusable_artist; None when the enumeration failed
    truncated: bool           # more than lastfm_tracks.MAX_ALBUMS albums existed (ArtistTracks.albums_truncated)
    counts: dict              # track counts of this artist (COUNT_KEYS, plus what run() counted)
    reason: str | None = None  # outcome `cut`: cap | time | sign-in


def lastfm_session_client(key: str, planned: int, ranking_calls: int = 0, **client_kwargs):
    """A Last.fm client whose request budget fits a bootstrap session (#166): LASTFM_ARTIST_CALLS per planned artist
    plus `ranking_calls` when the session also reads a ranking. Refuses (IngestError, one line) instead of falling
    back on the client default MAX_CALLS of 400, which is far too small for a list of artists."""
    import lastfm
    for label, v, low in (("planned artists", planned, 1), ("ranking calls", ranking_calls, 0)):
        if isinstance(v, bool) or not isinstance(v, int) or v < low:
            raise IngestError(f"lastfm session budget: {label} must be an integer of at least {low}, got {v!r}")
    return lastfm.LastfmClient(key, max_calls=planned * LASTFM_ARTIST_CALLS + ranking_calls, **client_kwargs)


def run_lastfm(lr: LastfmRun, cfg: LastfmConfig, artists: list[dict],
               enumerate_artist: Callable[[dict], "list[dict] | object"], stored_rows: list[tuple], deps: Deps,
               existing: set, covers: dict, work: Path, dry: bool, limits: RunLimits | None, state: RunState,
               on_artist: Callable[[dict, ArtistReport], None] | None = None,
               rescan_days: int | None = None) -> None:
    """Works through `artists` ([{"name", "mbid", "playcount"}, ...] in ranking order) and records in `lr`, with the
    guarantees of run_followed: excluded names and artists scanned less than cfg.rescan_days ago are skipped, a failed
    enumeration is logged and counted while the others go on, a sign-in challenge ends the run (raised after `lr` is
    updated), a limit ends the phase, and a scan is recorded only when it reached the last track (never in a dry run).
    `artists` is the ranking and `state` the state object, so a caller (the bootstrap, #168) injects its own ranking
    and its own state file; the `lastfm_artists:` section passes the live ranking and the nightly state.
    #166, both optional (None = behaviour as before): `rescan_days` replaces cfg.rescan_days (0 = the caller's list is
    what is worked, the scan dates of `state` decide nothing); `on_artist(artist, ArtistReport)` is called right after
    each artist (after the state write, before a stop is raised or returned), for an unusable name too. The
    enumerator may return the lastfm_tracks.ArtistTracks (real_lastfm_enumerator(full=True)): its status and
    truncation then reach the hook; a plain list counts as status ok. An exception of the hook is not caught."""
    import dedupe as dedupe_mod
    import lastfm
    if rescan_days is not None and (isinstance(rescan_days, bool) or not isinstance(rescan_days, int) or rescan_days < 0):
        raise ValueError("rescan_days must be a non-negative integer")
    days = cfg.rescan_days if rescan_days is None else rescan_days
    zero = {k: 0 for k in COUNT_KEYS}
    first: dict[str, dict] = {}
    for a in artists:
        if not normalize(a["name"]):  # "!!!" and "???" would share one state key: skipped and counted
            lr.unusable += 1
            if on_artist:
                on_artist(a, ArtistReport(lastfm_artist_key(a["name"]), "done", "unusable_artist", False, dict(zero)))
            continue
        first.setdefault(lastfm_artist_key(a["name"]), a)  # first (best ranked) occurrence wins
    lr.listed = len(first)
    excluded = {normalize(x) for x in cfg.exclude}
    wanted = {k: a for k, a in first.items() if normalize(a["name"]) not in excluded}
    lr.excluded = len(first) - len(wanted)
    due = [(k, a) for k, a in wanted.items() if state.artist_due(k, days)]
    lr.recent = len(wanted) - len(due)
    for n, (key, a) in enumerate(due):
        left = len(due) - n
        if limits and not limits.stop_reason and (reason := limits.exhausted()):
            limits.stop(reason)
        if limits and limits.stop_reason:
            lr.status, lr.not_started = "stopped", left
            log(f"stopped: {limits.stop_reason}: limit reached; {left} artist(s) not started; the next run continues")
            return
        label = printable(a["name"]).replace("::", ":?")  # no "::" in a log line from outside data
        try:
            got = enumerate_artist(a)
            songs = got.records if hasattr(got, "records") else got
            enum, truncated = (got.status, got.albums_truncated) if hasattr(got, "records") else ("ok", False)
            result = dedupe_mod.collapse(songs, collect_pairs=True)
        except (lastfm.LastfmError, IngestError, OSError, ValueError, TypeError) as e:
            detail = f": {e}" if isinstance(e, (lastfm.LastfmError, IngestError)) else ""
            log(f"ERROR artist {label}: enumeration failed: {type(e).__name__}{detail}")
            lr.failed += 1
            if on_artist:
                on_artist(a, ArtistReport(key, "failed", None, False, dict(zero)))
            continue
        present = {kept for gone, kept in result.pairs if gone in existing}
        kept = [r for r in result.kept if r["song_id"] not in present]
        feed = drop_stored_lastfm(kept, stored_rows)
        lr.dropped_stored += len(kept) - len(feed)
        log(f"artist {label}: {dedupe_mod.summary(result)}"
            + (f", {len(result.kept) - len(feed)} already stored" if len(feed) != len(result.kept) else ""))
        try:
            counts = run(feed, deps, existing, covers, work, dry, limits, state)
        except YouTubeSignInRequired as e:
            lr.add(e.counts or {})
            lr.status, lr.not_started, lr.not_attempted = "interrupted", left - 1, e.tracks_not_attempted + 1
            log(f"artist {label}: interrupted by youtube-sign-in: {summary(e.counts or {k: 0 for k in COUNT_KEYS})}; "
                f"{left - 1} artist(s) not started")
            if on_artist:
                on_artist(a, ArtistReport(key, "cut", enum, truncated, {**zero, **(e.counts or {})}, "sign-in"))
            raise
        counts["exists"] += len(result.kept) - len(feed)
        lr.add(counts)
        log(f"artist {label}: {summary(counts)}")
        if limits and limits.stop_reason:  # a limit ended the artist early: not recorded, the next run resumes here
            lr.status, lr.not_started, lr.not_attempted = "stopped", left - 1, limits.not_attempted
            log_limit_stop_artists(limits, left - 1)
            if on_artist:
                on_artist(a, ArtistReport(key, "cut", enum, truncated, dict(counts), limits.stop_reason))
            return
        lr.scanned += 1
        if not dry:
            state.record_artist(key)
        if on_artist:
            on_artist(a, ArtistReport(key, "done", enum, truncated, dict(counts)))


def lastfm_summary(lr: LastfmRun) -> str:
    """One line of counts for the log."""
    parts = [f"{lr.listed} ranked", f"{lr.scanned} scanned", f"{lr.recent} scanned recently", f"{lr.excluded} excluded"]
    for n, what in ((lr.not_started, "not started"), (lr.failed, "failed"), (lr.dropped_stored, "already stored")):
        if n:
            parts.append(f"{n} {what}")
    return f"lastfm artists: {', '.join(parts)}; tracks: {summary(lr.counts)}"


# ---- nightly new-release phase (#172) ----
# After the playlists: a bounded check of the artists of the STORED bootstrap ranking (bootstrap_ranking.read, never
# computed here, never the lastfm_artists section) that are due, with the detector of ingest/new_releases.py (#171),
# and the download of the tracks it finds through the same run() as a playlist. Contract: docs/bootstrap-ongoing.md
# section 4. The shipped ingest.log gets COUNTS and hashed keys only: no artist, no release, no track title (run() is
# called with quiet=True, the detector logs the 16-hex key). Imported lazily: a run without the section never loads
# new_releases.py, bootstrap_ranking.py or the Last.fm modules they import.

NEW_RELEASES_PHASE_MINUTES = 20  # wall clock of the phase per night (accepted in #165), within the run's --max-minutes
NEW_RELEASES_TRACK_STATUS = {"new": "stored", "exists": "present", "no_match": "no_match", "too_big": "too_big"}


@dataclass
class NewReleasesRun:
    """What the new-release phase did; filled while it runs, so it is complete after a sign-in stop too. Counts only.
    due = the artists the night planned (due, cut to max_artists): checked + skipped + failed + not_started == due."""
    counts: dict = field(default_factory=lambda: {"new": 0, "exists": 0, "no_match": 0, "too_big": 0, "dry_run": 0,
                                                  "failed": 0, "cooldown": 0})
    due: int = 0
    checked: int = 0            # checked, the check went through (also with nothing new)
    with_new: int = 0           # checked and found tracks that are not stored
    skipped: int = 0            # not resolvable on the source (no exact name, several artists) or unusable
    failed: int = 0             # the check failed or was refused (backs off in the state of the detector)
    not_started: int = 0        # not started: a limit, a refusal or a sign-in stop; they stay due
    not_attempted: int = 0      # tracks of the artist in progress that were not looked at
    status: str = "done"        # done | stopped | interrupted | failed | no_ranking

    def add(self, counts: dict) -> None:
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v

    def line(self) -> str:
        return (f"new releases: {self.due} due, {self.checked} checked, {self.with_new} with new, "
                f"{self.counts['new']} added, {self.failed} failed, {self.skipped} skipped, "
                f"{self.not_started} not started; tracks: {summary(self.counts)}")


def _new_releases_stop(nr: NewReleasesRun, not_started: int, what: str) -> None:
    nr.status, nr.not_started = "stopped", nr.not_started + not_started
    log(f"stopped: new releases: {what}; {not_started} artist(s) not started; the next run continues")


def run_new_releases_phase(nr: NewReleasesRun, cfg: NewReleasesConfig, source, deps: Deps, existing: set,
                           covers: dict, work: Path, dry: bool, limits: RunLimits, track_state: RunState,
                           state_path: Path, ranking_path: Path | None, pacer=None) -> None:
    """Ranking -> due artists -> per artist: check, then run() on the new tracks. Every failure that is not a sign-in
    challenge is counted and logged by class (no names) and does not stop the playlists, which are done by now. A
    sign-in challenge ends the run (raised after the state of the detector and `nr` are updated). `pacer` is the
    detector's pacer (None: its shared one, at least 1 s between source calls). Dry run: checks only, no state is
    written. An artist whose tracks were cut by a limit stays due (checked_at is put back), so the next night resumes."""
    import bootstrap_ranking
    import new_releases
    ranking, why = bootstrap_ranking.read(ranking_path)
    if ranking is None:  # nothing to do: the nightly never computes the ranking
        nr.status = "no_ranking"
        log(f"new releases: no usable stored ranking ({why}); nothing to do (run the bootstrap command first)")
        return
    try:
        nrstate = new_releases.load_state(state_path)
    except new_releases.NewReleasesStateError as e:  # names the file, never an artist; the file is not replaced
        nr.status = "failed"
        log(f"ERROR new releases: {e}")
        return
    today = track_state.today()
    selected, _ = new_releases.select_due([a["name"] for a in ranking["artists"]], nrstate, today, cfg.max_artists,
                                          cfg.recheck_days)
    nr.due = len(selected)
    if not selected:
        return
    if limits.stop_reason is None and (reason := limits.exhausted()):
        limits.stop(reason)
    if limits.stop_reason:  # a heavy playlist night left no budget: the artists stay due
        _new_releases_stop(nr, len(selected), f"{limits.stop_reason}: limit reached before the phase started")
        return
    try:
        stored_rows, stored_albums = read_stored_rows(d1_query), read_stored_albums(d1_query)
    except (IngestError, OSError, KeyError, TypeError, ValueError) as e:
        nr.status = "failed"
        log(f"ERROR new releases: cannot read the stored library ({type(e).__name__})")
        return
    pl = PhaseLimits(limits, cfg.max_new, NEW_RELEASES_PHASE_MINUTES)
    for n, name in enumerate(selected):
        left = len(selected) - n
        if pl.stop_reason is None and (reason := pl.exhausted()):
            pl.stop(reason)
        if pl.stop_reason:
            _new_releases_stop(nr, left, f"{pl.stop_reason}: limit reached")
            break
        key = new_releases.artist_key(name)
        before = dict(nrstate["artists"].get(key) or {})
        try:
            res = new_releases.check_artist(source, name, nrstate, stored_rows,
                                            stored_albums.get(normalize(name), []), today=today, pacer=pacer,
                                            recheck_days=cfg.recheck_days, out=log)
        except Exception as e:  # noqa: BLE001 - a bug or a surprise of the detector must not end the run after the playlists
            log(f"ERROR new releases {key}: the check failed ({type(e).__name__})")
            nr.failed += 1
            continue
        if res.status == new_releases.OK:
            nr.checked += 1
            nr.with_new += bool(res.records)
        elif res.status in (new_releases.FAILED, new_releases.REFUSED):
            nr.failed += 1
        else:
            nr.skipped += 1
        outcomes: dict = {}
        interrupted: YouTubeSignInRequired | None = None
        crashed = False
        if res.records:
            try:
                nr.add(run(res.records, deps, existing, covers, work, dry, pl, track_state, outcomes, quiet=True))
            except YouTubeSignInRequired as e:
                interrupted = e
                nr.add(e.counts or {})
            except Exception as e:  # noqa: BLE001 - a surprise must not end the run after the playlists; class only, no text
                crashed = True
                nr.failed += 1
                log(f"ERROR new releases {key}: tracks failed unexpectedly ({type(e).__name__}); phase ended")
        if not dry:
            statuses = {sid: NEW_RELEASES_TRACK_STATUS.get(o, "failed") for sid, o in outcomes.items()}
            new_releases.mark_outcomes(nrstate, res, statuses)
            if pl.stop_reason or interrupted:  # cut: the artist stays due, its unfinished releases are not evaluated
                entry = nrstate["artists"].get(key)
                if entry is not None and res.status == new_releases.OK:
                    if before.get("checked_at"):
                        entry["checked_at"] = before["checked_at"]
                    else:
                        entry.pop("checked_at", None)
            try:
                new_releases.save_state(state_path, nrstate)
            except (new_releases.NewReleasesStateError, OSError) as e:
                log(f"ERROR new releases: cannot write the state file ({type(e).__name__}); phase ended")
                if not interrupted and not crashed:
                    nr.status, nr.not_started = "failed", nr.not_started + left - 1
                    break
        if crashed:  # state is saved above; the rest of the artists stay due
            nr.status, nr.not_started = "failed", nr.not_started + left - 1
            break
        if interrupted:
            nr.status, nr.not_started = "interrupted", nr.not_started + left - 1
            nr.not_attempted = interrupted.tracks_not_attempted + 1
            log(f"new releases {key}: interrupted by youtube-sign-in; {left - 1} artist(s) not started")
            raise interrupted
        if res.status == new_releases.REFUSED:  # the source's first refusal ends the phase: no retry, no bypass
            _new_releases_stop(nr, left - 1, "the source refused the request")
            break
        if pl.stop_reason:  # a limit ended the artist early
            nr.not_attempted = pl.not_attempted
            _new_releases_stop(nr, left - 1, f"{pl.stop_reason}: limit reached")
            break


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
# Added in #152 (additive, only when sources.yml has an enabled lastfm_artists section): `lastfm_artists` = the same
# shape, artists: {listed, excluded, skipped_recent, scanned, failed, not_started, dropped_stored, unusable}; status
# `failed` when the ranking or the stored-rows read failed (exit 1, or 2 for a refusal only the owner can fix); its counts (and
# its `matched_title_only`) are also part of `counts`.
# Added in #172 (additive, only when sources.yml has an enabled new_releases section): `new_releases` = {status: done |
# stopped | interrupted | failed | no_ranking, due, checked, with_new, added, skipped, failed, not_started}: counts of
# artists (added = tracks stored); the track counts of the phase are also part of `counts`. No name, no id.
RESULT_COUNT_KEYS = ("added", "skipped", "no_match", "too_big", "failed", "not_attempted", "cooldown")


def _result_counts(c: dict | None, not_attempted: int = 0) -> dict:
    c = c or {}
    return {"added": c.get("new", 0), "skipped": c.get("exists", 0), "no_match": c.get("no_match", 0),
            "too_big": c.get("too_big", 0), "failed": c.get("failed", 0), "not_attempted": not_attempted,
            "cooldown": c.get("cooldown", 0),
            **({"matched_title_only": c["matched_title_only"]} if c.get("matched_title_only") else {}),
            **({"matched_normalized": c["matched_normalized"]} if c.get("matched_normalized") else {})}


def stop_reason(results: dict | None, interrupted: YouTubeSignInRequired | None, limits: RunLimits | None) -> str | None:
    """Why the run ended: sign-in, cap, time, or done (it ran through); None when it never started running."""
    if interrupted:
        return "sign-in"
    if limits and limits.stop_reason:
        return limits.stop_reason
    return "done" if results is not None else None


def build_result(urls: list[str], results: dict | None, interrupted: YouTubeSignInRequired | None,
                 exit_code: int, limits: RunLimits | None = None, followed: FollowedRun | None = None,
                 lastfm: LastfmRun | None = None, new_releases: NewReleasesRun | None = None) -> dict:
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
    lastfm_section = None
    if lastfm:  # additive (#152): only counts, never an artist name or id
        lastfm_section = {"status": lastfm.status, **_result_counts(lastfm.counts, lastfm.not_attempted),
                          "artists": {"listed": lastfm.listed, "excluded": lastfm.excluded,
                                      "skipped_recent": lastfm.recent, "scanned": lastfm.scanned,
                                      "failed": lastfm.failed, "not_started": lastfm.not_started,
                                      "dropped_stored": lastfm.dropped_stored, "unusable": lastfm.unusable}}
        counts = {k: counts[k] + lastfm_section[k] for k in RESULT_COUNT_KEYS}
    nr_section = None
    if new_releases:  # additive (#172): only counts, never an artist name or id
        nr_counts = _result_counts(new_releases.counts, new_releases.not_attempted)
        nr_section = {"status": new_releases.status, "due": new_releases.due, "checked": new_releases.checked,
                      "with_new": new_releases.with_new, "added": new_releases.counts["new"],
                      "skipped": new_releases.skipped, "failed": new_releases.failed,
                      "not_started": new_releases.not_started}
        counts = {k: counts[k] + nr_counts[k] for k in RESULT_COUNT_KEYS}
    title_only = sum(p.get("matched_title_only", 0)
                     for p in (*playlists, *[x for x in (section, lastfm_section) if x]))
    if new_releases:
        title_only += nr_counts.get("matched_title_only", 0)
    if title_only:  # additive (#151): present only when the title-only matcher mode matched something
        counts["matched_title_only"] = title_only
    normalized = sum(p.get("matched_normalized", 0)
                     for p in (*playlists, *[x for x in (section, lastfm_section) if x]))
    if new_releases:
        normalized += nr_counts.get("matched_normalized", 0)
    if normalized:  # additive (#194): present only when the relaxed title pass chose a match
        counts["matched_normalized"] = normalized
    counts["playlists_unreadable"] = sum(p["status"] == "unreadable" for p in playlists)
    counts["playlists_not_attempted"] = sum(p["status"] == "not_attempted" for p in playlists)
    if exit_code == 0:
        cls = "none"
    elif exit_code == EXIT_YOUTUBE_SIGN_IN:
        cls = FAILURE_YOUTUBE_SIGN_IN
    elif exit_code == EXIT_CONFIG:  # only the Last.fm ranking is refused after the playlists ran (#152)
        cls = "config"
    elif results is not None:
        cls = "partial"
    else:
        cls = "config"
    result = {"failure_class": cls, "exit_code": exit_code, "stop_reason": stop_reason(results, interrupted, limits),
              "playlists": playlists, "counts": counts}
    if section:
        result["followed_artists"] = section
    if lastfm_section:
        result["lastfm_artists"] = lastfm_section
    if nr_section:
        result["new_releases"] = nr_section
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
    state: dict = {"urls": [], "results": None, "interrupted": None, "limits": None, "followed": None, "lastfm": None,
                   "new_releases": None}
    code = execute(args, state)
    if args.result_json:
        write_result(args.result_json, build_result(state["urls"], state["results"], state["interrupted"], code,
                                                    state["limits"], state["followed"], state["lastfm"],
                                                    state["new_releases"]))
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


def prepare_lastfm(cfg: LastfmConfig, sources: Path | None):
    """(client, 0), or (None, EXIT_CONFIG) after logging why the Last.fm source cannot run. Offline checks only: no
    or a malformed LASTFM_API_KEY, an invalid user name. Nothing is fetched here: the ranking is read by
    run_lastfm_phase AFTER the playlists, so a Last.fm problem can never stop them. Messages name the variable,
    never its value. Imports ingest/lastfm.py lazily (only here)."""
    import lastfm
    try:
        key = lastfm.api_key_from_env()
        lastfm.parse_user(cfg.user)
        # one client for the run: the ranking needs about 53 requests per year, each artist has its own budget
        budget = 1 + cfg.years * lastfm.WEEKS_PER_YEAR_BOUND + 4 + cfg.max_artists * LASTFM_ARTIST_CALLS
        return lastfm.LastfmClient(key, max_calls=budget, out=log), 0
    except lastfm.LastfmError as e:
        log(f"ERROR lastfm_artists is enabled in {sources} but cannot run: {e}; set LASTFM_API_KEY in .env "
            f"(docs/local-ingest.md) and check `user`, or remove or comment out the lastfm_artists section "
            f"(for a manual ingest.py run `enabled: false` also works; the nightly wrapper refuses the section, the "
            f"ranking run goes through the bootstrap command, docs/bootstrap-ongoing.md); exit code {EXIT_CONFIG}")
        return None, EXIT_CONFIG


class RankingStopped(Exception):
    """--max-minutes ran out while the ranking was being read."""


class DeadlineClient:
    """Wraps the Last.fm client for the ranking only: every request first checks the run's limits, so --max-minutes
    also bounds the ranking (about 260 requests at 1 per second)."""

    def __init__(self, client, limits: "RunLimits | None"):
        self._client, self._limits = client, limits

    def call(self, method: str, **params):
        if self._limits and self._limits.exhausted():
            raise RankingStopped()
        return self._client.call(method, **params)


LASTFM_CONFIG_CODES = (6, 10, 17, 26)  # unknown user, invalid key, private/refused profile, suspended key
LASTFM_EMPTY_PROFILE = ("no weekly charts in the window", "the weekly charts hold no artists")  # lastfm.top_artists


def lastfm_config_problem(e: Exception) -> bool:
    """True for a refusal only the owner can fix (exit 2): Last.fm codes 6, 10, 17, 26, or an empty/private profile.
    An outage, 5xx, rate limit or malformed answer is a failure of the phase (exit 1) instead."""
    return getattr(e, "code", None) in LASTFM_CONFIG_CODES or str(e).startswith(LASTFM_EMPTY_PROFILE)


def run_lastfm_phase(lr: LastfmRun, cfg: LastfmConfig, client, deps: Deps, existing: set, covers: dict, work: Path,
                     dry: bool, limits: RunLimits, state: RunState) -> None:
    """The Last.fm phase, run after the playlists and followed artists: ranking, stored rows, artists. Every failure
    that is not a sign-in challenge is logged (redacted, one line) and ends the phase with status `failed`; the
    playlist results and everything already uploaded stay. `lr.config_error` marks a refusal for exit 2."""
    import lastfm
    key = os.environ.get("LASTFM_API_KEY", "")
    if limits.stop_reason or limits.exhausted():  # no point in a 5 minute ranking
        lr.status = "stopped"
        log("stopped: limit reached before the Last.fm ranking was read; the next run continues")
        return
    try:
        ranking = lastfm.top_artists(DeadlineClient(client, limits), cfg.user, cfg.years, max_artists=cfg.max_artists,
                                     out=log)
    except RankingStopped:
        limits.stop("time")
        lr.status = "stopped"
        log("stopped: time: --max-minutes limit reached while the Last.fm ranking was read; the next run continues")
        return
    except (lastfm.LastfmError, OSError) as e:
        lr.status = "failed"
        text = redact(str(e), [key])
        if lastfm_config_problem(e):
            lr.config_error = True
            log(f"ERROR cannot read the Last.fm ranking: {text}; check LASTFM_API_KEY, the user name and that the "
                f"profile is public, or remove or comment out the lastfm_artists section (for a manual ingest.py run "
                f"`enabled: false` also works; the ranking run goes through the bootstrap command, "
                f"docs/bootstrap-ongoing.md); exit code {EXIT_CONFIG}")
        else:
            log(f"ERROR cannot read the Last.fm ranking: {text}; the playlists are not affected; the next run tries again")
        return
    log(f"lastfm artists: {len(ranking)} ranked over the last {cfg.years} year(s)")
    try:
        run_lastfm(lr, cfg, ranking, real_lastfm_enumerator(client), read_stored_rows(d1_query), deps, existing,
                   covers, work, dry, limits, state)
    except YouTubeSignInRequired:
        raise
    except (IngestError, OSError) as e:  # for example a D1 error while reading the stored rows
        lr.status = "failed"
        log(f"ERROR lastfm phase failed: {type(e).__name__}: {redact(str(e), [key])}")


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

    lastfm_cfg = config.lastfm if config.lastfm and config.lastfm.enabled else None
    lastfm_client = None
    if lastfm_cfg:  # before any work, offline checks only: no or a malformed key, an invalid user is a config error
        lastfm_client, code = prepare_lastfm(lastfm_cfg, args.sources)
        if code:
            return code

    nr_cfg = config.new_releases if config.new_releases and config.new_releases.enabled else None

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
            ytm = YTMusic()
            deps = real_deps(ytm, throttle)
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
            if lastfm_cfg:
                lr = state["lastfm"] = LastfmRun()
                try:
                    run_lastfm_phase(lr, lastfm_cfg, lastfm_client, deps, existing, covers, work, args.dry_run,
                                     limits, track_state)
                except YouTubeSignInRequired as e:
                    e.results, e.playlists_not_attempted = results, 0
                    raise
            if nr_cfg:  # last: after the playlists (and the followed/lastfm phases), bounded by its own caps
                nr = state["new_releases"] = NewReleasesRun()
                state_file = args.state_file or default_state_path()
                try:
                    import bootstrap_ranking
                    run_new_releases_phase(nr, nr_cfg, ytm, deps, existing, covers, work, args.dry_run, limits,
                                           track_state, state_file.with_name("new-releases-state.json"),
                                           state_file.with_name(bootstrap_ranking.FILE_NAME))
                except YouTubeSignInRequired as e:
                    e.results, e.playlists_not_attempted = results, 0
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
    fr, lr, nr = state["followed"], state.get("lastfm"), state.get("new_releases")
    total = {k: sum(c.get(k, 0) for c in [*ok, *([fr.counts] if fr else []), *([lr.counts] if lr else []),
                                          *([nr.counts] if nr else [])])
             for k in (*COUNT_KEYS, "cooldown", "matched_title_only", "matched_normalized")}
    broken = len(results) - len(ok)
    enum_failed = (fr.failed if fr else 0) + (lr.failed if lr else 0)
    artists_failed = enum_failed + (nr.failed if nr else 0)
    phase_failed = bool(lr and lr.status == "failed")
    nr_failed = bool(nr and nr.status == "failed")
    if fr:
        log(followed_summary(fr))
    if lr:
        log(lastfm_summary(lr))
    if nr and nr.status != "no_ranking":  # no ranking: the one line of the phase was logged already
        log(nr.line())
    log(f"done: {summary(total)}" + (f"; {broken} playlist(s) could not be read" if broken else "")
        + (f"; {enum_failed} artist(s) could not be enumerated" if enum_failed else "")
        + (f"; {nr.failed} new-release check(s) failed" if nr and nr.failed else "")
        + ("; the Last.fm phase failed" if phase_failed else "")
        + ("; the new-release phase failed" if nr_failed else ""))
    log(f"stop reason: {stop_reason(results, None, limits)}")
    if lr and lr.config_error:  # the playlists ran and are reported; the owner must fix the Last.fm side
        return EXIT_CONFIG
    return 1 if broken or total["failed"] or artists_failed or phase_failed or nr_failed else 0


if __name__ == "__main__":
    sys.exit(main())

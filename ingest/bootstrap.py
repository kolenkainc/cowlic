"""The Release 3 bootstrap command (#168): a manual, capped, resumable job that works through the stored Last.fm ranking
with the shared ingest engine (ingest.run_lastfm) and keeps its own progress. Contract: docs/bootstrap-ongoing.md.

  bootstrap.py rank   [--config FILE] [--refresh]
  bootstrap.py plan   --max-new N --max-minutes M [--config FILE]
  bootstrap.py run    --max-new N --max-minutes M [--config FILE] [--result-json FILE] [--reset-state] [--unpark]
  bootstrap.py status [--config FILE]
  bootstrap.py covers --max-new N --max-minutes M [--retry-days D] [--retry-failed-days D] [--result-json FILE]
  bootstrap.py covers --count [--retry-days D] [--retry-failed-days D]
  bootstrap.py covers --artists --max-new N --max-minutes M [--retry-days D] [--retry-failed-days D] [--result-json FILE]
  bootstrap.py covers --artists --count [--retry-days D] [--retry-failed-days D]

  rank    computes the ranking once (Last.fm) and stores it (bootstrap_ranking); refuses to recompute without --refresh.
  plan    dry run: which artists the next session would reach and how many matches it would attempt. Downloads nothing,
          uploads nothing, writes no file (no state, no lock). Still reads D1 and searches YouTube Music.
  run     one session. Both caps are REQUIRED (1..1000000 tracks, 1..1440 minutes). Never computes the ranking.
  status  one or two lines, no network, no names, no titles; exit 0.
  covers  (#250) backfill the cover of albums stored without one: per cover-less album the shared matcher on one stored
          track, the cover guard and the hardened thumbnail fetch of #248, a Telegram photo and
          `UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL`. No audio. Both caps are REQUIRED
          (--max-new counts albums attempted); the work list comes from D1, so a stopped run resumes by itself; albums
          without a match or with a mismatch rest for --retry-days (default 30), failed ones for --retry-failed-days (default 3), in cover-backfill.json. Takes the shared
          lock and has the 21:10 guard. Never reads the ranking or the progress file; needs no Last.fm key and no config.
          --count is read-only (D1 reads only, no YouTube, no Telegram, no write), takes no caps, has no 21:10 guard and
          prints how many albums and artists lack a cover and how many rest in the cooldown. Logic: bootstrap_covers.py.
          --artists (#253) switches to the artists stored without a photo (image_file_id IS NULL): the shared matcher, the
          ingest artist guard, one get_artist, the hardened fetch, `UPDATE artists SET image_file_id = ? WHERE id = ? AND
          image_file_id IS NULL`; cooldown in artist-photo-backfill.json; a missing column is a clean skip (exit 0).
          Logic: bootstrap_artist_photos.py.

Files (state directory ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/, directory 0700, files 0600, atomic writes):
  bootstrap-ranking.json   written by `rank` only (bootstrap_ranking.py)
  bootstrap-state.json     the ENGINE's own RunState (cooldowns, scan dates); this file is never written by bootstrap
                           itself, only by the engine; the nightly ingest-state.json is never read or written
  cover-backfill.json      written by `covers` only (bootstrap_covers.py): album id -> {result no_match|mismatch|failed, on date}
  bootstrap-progress.json  the single truth about each artist, written only here: {version, ranking_computed_at,
                           sessions, artists: {<lfm:hash>: {status, enum, truncated, tracks_added, tracks_seen,
                           failures, updated}}}; status pending | partial | done | parked, an absent key is pending.
                           Ids, hashes, words and counts only: no names, no titles, no Last.fm user.

Config (a LOCAL file outside the repo, --config or ${XDG_CONFIG_HOME:-$HOME/.config}/cowlic/bootstrap.yml): the
`lastfm_artists:` section parsed by the shared strict parser of ingest.py. `rank` needs user, years, max_artists; `run`,
`plan` and `status` use only `exclude`; `enabled` and `rescan_days` have no meaning here (the shared parser still
validates them, so `enabled: false` or a file without `user` is refused as a bad config). sources.yml is not used. The user
name is read by `rank` only and goes nowhere but to Last.fm.

Environment: LASTFM_API_KEY (never printed) and, for run, the same variables as ingest.py; loaded from ./.env by the
existing loader. COWLIC_LOCK_HELD is read from the REAL process environment before any .env load; a value from .env is
ignored.

Exit codes: 0 ok (a cap or time stop is not a failure), 1 some track or artist failed or a write failed, 2 bad usage,
bad config, missing environment, no usable ranking, unusable state file, 3 YouTube sign-in stop (never bypassed:
no cookies, proxies, PO-token tricks, client rotation), 75 another process holds ingest.lock, 78 the session would run
into the next 21:10 timer run.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import bootstrap_artist_photos
import bootstrap_covers
import bootstrap_ranking
import ingest
import lastfm
import tg_check
from match import normalize

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
EXIT_SIGN_IN = ingest.EXIT_YOUTUBE_SIGN_IN  # 3
EXIT_LOCKED, EXIT_GUARD = 75, 78

PROGRESS_FILE = "bootstrap-progress.json"
STATE_FILE = "bootstrap-state.json"
LOCK_FILE = "ingest.lock"
PROGRESS_VERSION = 1
PROGRESS_MAX_BYTES = 10 * 1024 * 1024
PROGRESS_MAX_ARTISTS = 100000
CONFIG_MAX_BYTES = 256 * 1024
MAX_FAILURES = 3  # consecutive enumeration failures after which an artist is parked
MAX_NEW_RANGE = (1, 1_000_000)
MAX_MINUTES_RANGE = (1, 1440)
GUARD_MARGIN_MINUTES = 10  # plus the timer's RandomizedDelaySec (60 s)
GUARD_TIMER_DELAY_SECONDS = 60
TIMER_HOUR, TIMER_MINUTE = 21, 10  # deploy/systemd/cowlic-ingest.timer OnCalendar, local time
STATUSES = ("pending", "partial", "done", "parked")
ENUMS = ("ok", "unknown_artist", "no_albums", "unusable_artist")  # lastfm_tracks.ArtistTracks statuses + unusable
EMPTY_ENUMS = ("unknown_artist", "no_albums", "unusable_artist")
ENTRY_KEYS = frozenset({"status", "enum", "truncated", "tracks_added", "tracks_seen", "failures", "updated"})
PROGRESS_KEYS = frozenset({"version", "ranking_computed_at", "sessions", "artists"})
HANDLED_KEYS = ("new", "exists", "no_match", "too_big", "failed", "cooldown")  # counted per artist as tracks_seen
D1_ENV = ["INGEST_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID"]
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_COUNT_MAX = 10 ** 9


class StateError(Exception):
    """A bootstrap state file that must not be used. The message is a reason without names or file content."""


class Locked(Exception):
    """ingest.lock is held by another process."""


class ConfigError(Exception):
    """The local config file is missing, unreadable or invalid. The message names a reason, never the user name."""


# ---- small helpers ----

def state_dir() -> Path:
    return ingest.default_state_path().parent


def today() -> datetime.date:
    return ingest.utc_today()


def local_now() -> datetime.datetime:
    """Naive local time (the timer's OnCalendar is local time). Tests replace it."""
    return datetime.datetime.now()


def make_limits(max_new: int, max_minutes: int) -> ingest.RunLimits:
    return ingest.RunLimits(max_new, max_minutes)


def refuse(message: str) -> None:
    """One stderr line for a refusal that happens before any work."""
    print(f"bootstrap: {message}", file=sys.stderr)


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _iso_date(value) -> bool:
    if not (isinstance(value, str) and _DATE.fullmatch(value)):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


# ---- the lock (docs/bootstrap-ongoing.md 2.1 and 5) ----

def take_lock(directory: Path) -> int:
    """flock LOCK_EX | LOCK_NB on ingest.lock; the descriptor stays open for the life of the process (and is not
    inherited by children: os.open descriptors are non-inheritable). Raises Locked when another process holds it."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Locked() from None
    except OSError:
        os.close(fd)
        raise
    return fd


# ---- the 21:10 guard ----

def overlap_problem(now: datetime.datetime, max_minutes: int) -> str | None:
    """None when a session of `max_minutes` started `now` (naive local time) ends before the next 21:10 timer run,
    with a margin of 10 minutes plus the timer's 60 s random delay; else one line saying how many minutes fit. Same
    arithmetic as scripts/run-bootstrap-local.sh: within 60 s after 21:10 that day's run may still be about to start,
    so it counts as the next one."""
    start = now.timestamp()
    day = now.replace(hour=TIMER_HOUR, minute=TIMER_MINUTE, second=0, microsecond=0)
    if start > day.timestamp() + GUARD_TIMER_DELAY_SECONDS:
        day += datetime.timedelta(days=1)
    nxt = day.timestamp()
    margin = GUARD_MARGIN_MINUTES * 60 + GUARD_TIMER_DELAY_SECONDS
    if start + max_minutes * 60 + margin >= nxt:
        fit = max(0, int((nxt - start - margin - 1) // 60))
        return (f"refusing to start: now + --max-minutes {max_minutes} + {GUARD_MARGIN_MINUTES} min margin reaches the "
                f"next {TIMER_HOUR}:{TIMER_MINUTE:02d} local time (at most {fit} minutes fit now)")
    return None


# ---- strict reading of the state files ----

def read_json_file(path: Path, max_bytes: int):
    """The parsed JSON of a state file, or None when it does not exist. Raises StateError (reason only) for an
    unreadable, non-regular, oversized or invalid file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StateError(f"cannot be opened ({type(e).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StateError("not a regular file")
        if st.st_size > max_bytes:
            raise StateError(f"larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(max_bytes + 1)
    except OSError as e:
        raise StateError(f"cannot be read ({type(e).__name__})") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise StateError(f"larger than {max_bytes} bytes")

    def reject(_name):
        raise ValueError("non-finite number")
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise StateError("not valid JSON") from None


def check_engine_state(doc) -> None:
    """Stricter than RunState.load, which starts empty and drops what it does not understand: here ANY unknown field,
    unknown version or malformed entry refuses the file (None, a missing file, is fine)."""
    if doc is None:
        return
    if not isinstance(doc, dict):
        raise StateError("not a JSON object")
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateError("no valid version")
    if version != ingest.STATE_VERSION:
        raise StateError(f"unknown version {version}")
    if "tracks" not in doc or set(doc) - {"version", "tracks", "artists"}:
        raise StateError("missing or unknown fields")
    tracks, artists = doc["tracks"], doc.get("artists", {})
    if not isinstance(tracks, dict) or not isinstance(artists, dict):
        raise StateError("tracks or artists is not an object")
    for sid, e in tracks.items():
        if not (ingest.SONG_ID.fullmatch(sid) and isinstance(e, dict) and set(e) == {"status", "retry_after"}
                and e["status"] in ingest.COOLDOWN_STATUSES and _iso_date(e["retry_after"])):
            raise StateError("a track entry is malformed")
    for aid, day in artists.items():
        if not ((ingest.ARTIST_ID.fullmatch(aid) or ingest.LASTFM_ARTIST_KEY.fullmatch(aid)) and _iso_date(day)):
            raise StateError("an artist entry is malformed")


def _empty_entry(day: str) -> dict:
    return {"status": "pending", "enum": "ok", "truncated": False, "tracks_added": 0, "tracks_seen": 0,
            "failures": 0, "updated": day}


class Progress:
    """bootstrap-progress.json: the single truth about each artist. Written only by bootstrap.py."""

    def __init__(self, path: Path):
        self.path = path
        self.ranking_computed_at: str | None = None
        self.sessions = 0
        self.artists: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path) -> "Progress":
        """The progress, empty when the file does not exist; StateError when it is not entirely valid."""
        doc = read_json_file(path, PROGRESS_MAX_BYTES)
        p = cls(path)
        if doc is None:
            return p
        if not isinstance(doc, dict):
            raise StateError("not a JSON object")
        version = doc.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise StateError("no valid version")
        if version != PROGRESS_VERSION:
            raise StateError(f"unknown version {version}")
        if set(doc) != PROGRESS_KEYS:
            raise StateError("missing or unknown fields")
        if not (isinstance(doc["ranking_computed_at"], str) and _STAMP.fullmatch(doc["ranking_computed_at"])):
            raise StateError("ranking_computed_at is not a UTC timestamp")
        if not _int_in(doc["sessions"], 0, _COUNT_MAX):
            raise StateError("sessions is not a count")
        if not isinstance(doc["artists"], dict) or len(doc["artists"]) > PROGRESS_MAX_ARTISTS:
            raise StateError("artists is not an object of a sane size")
        for key, e in doc["artists"].items():
            ok = (ingest.LASTFM_ARTIST_KEY.fullmatch(key) and isinstance(e, dict) and set(e) == ENTRY_KEYS
                  and e["status"] in STATUSES and e["enum"] in ENUMS and isinstance(e["truncated"], bool)
                  and all(_int_in(e[k], 0, _COUNT_MAX) for k in ("tracks_added", "tracks_seen", "failures"))
                  and _iso_date(e["updated"]))
            if not ok:
                raise StateError("an artist entry is malformed")
        p.ranking_computed_at, p.sessions = doc["ranking_computed_at"], doc["sessions"]
        p.artists = {k: dict(e) for k, e in doc["artists"].items()}
        return p

    def status(self, key: str) -> str:
        e = self.artists.get(key)
        return e["status"] if e else "pending"

    def entry(self, key: str, day: str) -> dict:
        """The stored entry of `key`, created as pending when absent."""
        return self.artists.setdefault(key, _empty_entry(day))

    def to_doc(self) -> dict:
        return {"version": PROGRESS_VERSION, "ranking_computed_at": self.ranking_computed_at,
                "sessions": self.sessions, "artists": self.artists}

    def save(self) -> None:
        ingest.atomic_write_json(self.path, self.to_doc())  # temp + fsync + rename, 0600, directory 0700; OSError

    def unpark(self) -> int:
        n = 0
        for e in self.artists.values():
            if e["status"] == "parked":
                e["status"], e["failures"], n = "pending", 0, n + 1
        return n


def move_aside(path: Path) -> Path:
    """Move `path` to <name>.bak, or .bak.1, .bak.2, ... when that exists: never deletes, never overwrites (a hard
    link fails on an existing target, unlike rename)."""
    n = 0
    while True:
        dst = path.with_name(path.name + (".bak" if n == 0 else f".bak.{n}"))
        try:
            os.link(path, dst)
            break
        except FileExistsError:
            n += 1
    os.unlink(path)
    return dst


# ---- the per-artist hook ----

class Recorder:
    """The on_artist hook of run_lastfm: writes the status of each artist into the progress file right after it. It
    NEVER raises: in the sign-in branch the engine calls the hook right before re-raising YouTubeSignInRequired, and an
    exception of the hook would replace it and lose exit code 3. A failure is logged (a type name, no content), counted
    in `errors`, and the in-memory state stays complete for the next write."""

    def __init__(self, progress: Progress, day: Callable[[], datetime.date] | None = None):
        self.progress, self.day = progress, day or (lambda: today())
        self.errors = 0
        self.unsaved = False
        self.completed = self.cut = self.failed = self.parked = 0

    def on_artist(self, artist: dict, report: "ingest.ArtistReport") -> None:
        try:
            self._apply(report)
            self.progress.save()
            self.unsaved = False
        except OSError as e:  # the write failed: the in-memory state is complete, the next artist's write retries
            self.unsaved = True
            ingest.log(f"WARN could not write {PROGRESS_FILE} ({type(e).__name__}); the next artist retries")
        except Exception as e:  # noqa: BLE001 - see the class docstring; logged, counted, never silent
            self.errors += 1
            ingest.log(f"ERROR progress hook failed: {type(e).__name__}")

    def _apply(self, report: "ingest.ArtistReport") -> None:
        day = self.day().isoformat()
        e = self.progress.entry(report.key, day)
        e["updated"] = day
        if report.outcome == "failed":  # the enumeration failed: status unchanged unless it is parked now
            e["failures"] += 1
            self.failed += 1
            if e["failures"] >= MAX_FAILURES and e["status"] != "parked":
                e["status"] = "parked"
                self.parked += 1
            return
        counts = report.counts
        e["enum"], e["truncated"], e["failures"] = report.enum or "ok", bool(report.truncated), 0
        e["tracks_added"] += int(counts.get("new", 0))
        e["tracks_seen"] = max(e["tracks_seen"], sum(int(counts.get(k, 0)) for k in HANDLED_KEYS))
        if report.outcome == "done":
            e["status"] = "done"
            self.completed += 1
        else:  # cut by the cap, the time limit or a sign-in stop: the next session resumes it
            e["status"] = "partial"
            self.cut += 1


# ---- config ----

def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "cowlic" / "bootstrap.yml"


def load_config(arg: Path | None, required: bool) -> ingest.LastfmConfig | None:
    """The `lastfm_artists:` section of the local config file, or None when there is no file and it is not required.
    An explicit --config must exist. Anything but that section (playlists, followed_artists) is refused."""
    path = arg or default_config_path()
    if arg is None and not path.is_file():
        if required:
            raise ConfigError("no config file: pass --config FILE (a local file outside the repo with a lastfm_artists: section)")
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read(CONFIG_MAX_BYTES + 1)
        if len(raw) > CONFIG_MAX_BYTES:
            raise ConfigError("the config file is too large")
        cfg = ingest.parse_source_config(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"the config file cannot be read ({type(e).__name__})") from None
    except ingest.IngestError as e:
        raise ConfigError(f"bad config: {e}") from None
    if cfg.urls or cfg.followed or cfg.lastfm is None:
        raise ConfigError("the config file must hold the lastfm_artists: section and nothing else")
    return cfg.lastfm


def excluded_names(cfg: ingest.LastfmConfig | None) -> set[str]:
    return {normalize(x) for x in cfg.exclude} if cfg else set()


# ---- environment ----

def prepare_env(required: list[str]) -> str | None:
    """Loads ./.env (the real environment wins) and checks the variables: a message naming them, or None."""
    try:
        tg_check.load_env()
    except tg_check.EnvSyntaxError as e:
        return str(e)
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        return f"missing env vars: {', '.join(missing)}"
    try:
        lastfm.api_key_from_env()
    except lastfm.LastfmError as e:
        return str(e)
    return None


# ---- ranking, plan and status ----

def usable_ranking() -> tuple[dict | None, str]:
    return bootstrap_ranking.read(None)


def work_list(ranking: dict, progress: Progress, excluded: set[str]) -> list[dict]:
    """The ranking minus excluded, done and parked artists, in rank order: what the engine is handed."""
    seen, out = set(), []
    for a in ranking["artists"]:
        if a["key"] in seen or normalize(a["name"]) in excluded or progress.status(a["key"]) in ("done", "parked"):
            continue
        seen.add(a["key"])
        out.append(a)
    return out


def status_lines(ranking: dict, progress: Progress, excluded: set[str]) -> list[str]:
    counted = [a for a in ranking["artists"] if normalize(a["name"]) not in excluded]
    n = len(counted)
    st = [progress.status(a["key"]) for a in counted]
    done, parked = st.count("done"), st.count("parked")
    tracks = sum(e["tracks_added"] for e in progress.artists.values())
    k = f", {parked} parked" if parked else ""
    if done + parked == n:
        lines = [f"done: {n} artists, {tracks} tracks{k}"]
    else:
        nxt = next(a["rank"] for a, s in zip(counted, st) if s not in ("done", "parked"))
        lines = [f"partial: {done} of {n} artists done, {tracks} tracks, next: {nxt}{k}"]
    entries = [progress.artists[a["key"]] for a in counted if a["key"] in progress.artists]
    empty = sum(e["enum"] in EMPTY_ENUMS for e in entries)
    truncated = sum(e["truncated"] for e in entries)
    if empty or truncated:
        lines.append(f"empty: {empty} artists with no tracks found (unknown or no albums), truncated: {truncated} artists")
    return lines


def cmd_status(args) -> int:
    ranking, why = usable_ranking()
    if ranking is None:
        print(f"no usable ranking: {why}; run rank first")
        return EXIT_OK
    directory = state_dir()
    try:
        check_engine_state(read_json_file(directory / STATE_FILE, ingest.STATE_MAX_BYTES))
    except StateError as e:
        print(f"state file unusable: {STATE_FILE}: {e}; run --reset-state moves it aside and starts empty")
        return EXIT_OK
    try:
        progress = Progress.load(directory / PROGRESS_FILE)
    except StateError as e:
        print(f"state file unusable: {PROGRESS_FILE}: {e}; run --reset-state moves it aside and starts empty")
        return EXIT_OK
    try:
        excluded = excluded_names(load_config(args.config, required=False))
    except ConfigError as e:
        print(f"config unusable: {e}")
        return EXIT_OK
    for line in status_lines(ranking, progress, excluded):
        print(line)
    return EXIT_OK


# ---- the engine session shared by run and plan ----

@dataclass
class Backends:
    """Everything outside the process: D1 reads, YouTube Music matcher and downloader, Telegram, Last.fm. Tests pass fakes."""
    deps: ingest.Deps
    existing: set
    covers: dict
    stored_rows: list
    enumerate_artist: Callable[[dict], object]


def real_backends(planned: int) -> Backends:
    """The real network side. The Last.fm client budget is sized for the session (150 requests per planned artist),
    never the silent default of 400."""
    from ytmusicapi import YTMusic
    client = ingest.lastfm_session_client(lastfm.api_key_from_env(), planned, out=ingest.log)
    existing = {r["spotify_id"] for r in ingest.d1_query("SELECT spotify_id FROM tracks")}
    covers = {r["id"]: r["cover_file_id"] for r in ingest.d1_query("SELECT id, cover_file_id FROM albums")}
    stored = ingest.read_stored_rows(ingest.d1_query)
    throttle = ingest.Throttle(ingest.DEFAULT_PACE_SECONDS, ingest.DEFAULT_PACE_JITTER_SECONDS, ingest.DEFAULT_ATTEMPTS)
    deps = ingest.real_deps(YTMusic(), throttle)
    return Backends(deps, existing, covers, stored, ingest.real_lastfm_enumerator(client, full=True))


def result_counts(counts: dict, not_attempted: int = 0) -> dict:
    return {"added": counts.get("new", 0), "skipped": counts.get("exists", 0), "no_match": counts.get("no_match", 0),
            "too_big": counts.get("too_big", 0), "failed": counts.get("failed", 0),
            "matched_title_only": counts.get("matched_title_only", 0), "cooldown": counts.get("cooldown", 0),
            "not_attempted": not_attempted}


def sign_in_stop() -> None:
    ingest.log(f"ERROR {ingest.FAILURE_YOUTUBE_SIGN_IN}: YouTube asks to sign in (bot check); stopping the session, "
               f"not retrying; manual action needed: wait and run again later, do not add cookies; exit code {EXIT_SIGN_IN}")


def run_engine(planned: list[dict], cfg: ingest.LastfmConfig, backends: Backends, limits: ingest.RunLimits,
               state: ingest.RunState, hook: Callable | None, dry: bool, secrets: list[str]):
    """run_lastfm over `planned` with rescan_days=0 (the progress file, not the engine's scan dates, decides what is
    worked). Returns (LastfmRun, interrupted or None, error or None)."""
    lr = ingest.LastfmRun()
    interrupted = error = None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ingest.run_lastfm(lr, cfg, planned, backends.enumerate_artist, backends.stored_rows, backends.deps,
                              backends.existing, backends.covers, Path(tmp), dry, limits, state, on_artist=hook,
                              rescan_days=0)
        except ingest.YouTubeSignInRequired as e:
            interrupted = e
        except (ingest.IngestError, OSError) as e:  # for example a D1 error; redacted, one line
            error = f"{type(e).__name__}: {ingest.redact(str(e), secrets)}"
    return lr, interrupted, error


def secrets_now() -> list[str]:
    return [os.environ.get(k, "") for k in ("LASTFM_API_KEY", "TG_BOT_TOKEN", "INGEST_CLOUDFLARE_API_TOKEN")]


def prepare(args, required_env: list[str]):
    """Common start of run and plan: config, ranking, environment. Returns (ranking, excluded, config section or None)
    or an exit code."""
    try:
        section = load_config(args.config, required=False)
    except ConfigError as e:
        refuse(str(e))
        return EXIT_USAGE
    if section is None:
        ingest.log("no config file: nothing is excluded (pass --config FILE to exclude artists)")
    ranking, why = usable_ranking()
    if ranking is None:
        refuse(f"no usable ranking ({why}): run rank first")
        return EXIT_USAGE
    problem = prepare_env(required_env)
    if problem:
        refuse(problem)
        return EXIT_USAGE
    return ranking, excluded_names(section), section


def engine_config(ranking: dict, excluded_cfg: ingest.LastfmConfig | None) -> ingest.LastfmConfig:
    return ingest.LastfmConfig(enabled=True, user="", years=ranking["years"], max_artists=ranking["max_artists"],
                               rescan_days=0, exclude=excluded_cfg.exclude if excluded_cfg else ())


def cmd_plan(args) -> int:
    got = prepare(args, D1_ENV)
    if isinstance(got, int):
        return got
    ranking, excluded, _section = got
    directory = state_dir()
    try:
        progress = Progress.load(directory / PROGRESS_FILE)
        check_engine_state(read_json_file(directory / STATE_FILE, ingest.STATE_MAX_BYTES))
    except StateError as e:
        refuse(f"state file unusable: {e}; run --reset-state moves the files aside and starts empty")
        return EXIT_USAGE
    planned = work_list(ranking, progress, excluded)
    if not planned:
        print("plan: nothing to do: every ranked artist is done, parked or excluded")
        return EXIT_OK
    try:
        backends = real_backends(len(planned))
    except (ingest.IngestError, OSError, lastfm.LastfmError) as e:
        ingest.log(f"ERROR cannot start the plan: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        return EXIT_FAILED
    state = ingest.RunState.load(directory / STATE_FILE)
    state.path = None  # a dry run never writes, whatever the engine does
    reports: list = []
    limits = make_limits(args.max_new, args.max_minutes)
    cfg = engine_config(ranking, None)
    lr, interrupted, error = run_engine(planned, cfg, backends, limits, state, lambda a, r: reports.append(r), True,
                                        secrets_now())
    if interrupted:
        sign_in_stop()
        return EXIT_SIGN_IN
    ranks = {a["key"]: a["rank"] for a in ranking["artists"]}
    reached = sorted(ranks[r.key] for r in reports if r.key in ranks)
    complete = sum(r.outcome == "done" for r in reports)
    cut = sum(r.outcome == "cut" for r in reports)
    failed = sum(r.outcome == "failed" for r in reports)
    span = f" (rank positions {reached[0]}-{reached[-1]})" if reached else ""
    print(f"plan: the next session (--max-new {args.max_new}, --max-minutes {args.max_minutes}) would reach "
          f"{len(reports)} of {len(planned)} artists{span}: {complete} complete, {cut} cut by a limit, {failed} whose "
          f"enumeration failed; it would attempt {limits.attempted} track(s); nothing was downloaded, uploaded or written")
    if error:
        ingest.log(f"ERROR plan stopped early: {error}")
        return EXIT_FAILED
    return EXIT_FAILED if failed or lr.counts.get("failed") else EXIT_OK


def reset_state(directory: Path) -> str | None:
    """--reset-state: BOTH bootstrap state files (progress and engine state) are moved aside, never deleted, never
    over an existing backup. The ranking and the nightly ingest-state.json are not touched. An error text or None."""
    for name in (PROGRESS_FILE, STATE_FILE):
        path = directory / name
        if os.path.lexists(path):
            try:
                dst = move_aside(path)
            except OSError as e:
                return f"cannot move {name} aside ({type(e).__name__})"
            ingest.log(f"{name} moved to {dst.name}; starting empty")
    return None


def cmd_run(args, lock_held: bool) -> int:
    directory = state_dir()
    lock = None
    if not lock_held:
        try:
            lock = take_lock(directory)
        except Locked:
            refuse(f"another run or session holds {LOCK_FILE}; exiting without doing anything")
            return EXIT_LOCKED
        except OSError as e:
            refuse(f"cannot take the lock ({type(e).__name__})")
            return EXIT_USAGE
    try:
        return _run_locked(args, directory)
    finally:
        if lock is not None:
            os.close(lock)


def _run_locked(args, directory: Path) -> int:
    problem = overlap_problem(local_now(), args.max_minutes)
    if problem:
        refuse(problem)
        return EXIT_GUARD
    got = prepare(args, ingest.REQUIRED_ENV)
    if isinstance(got, int):
        return got
    ranking, excluded, section = got
    if args.reset_state:
        problem = reset_state(directory)
        if problem:
            refuse(problem)
            return EXIT_USAGE
    try:
        progress = Progress.load(directory / PROGRESS_FILE)
        check_engine_state(read_json_file(directory / STATE_FILE, ingest.STATE_MAX_BYTES))
    except StateError as e:
        refuse(f"state file unusable: {e}; run --reset-state moves the files aside and starts empty")
        return EXIT_USAGE
    unparked = progress.unpark() if args.unpark else 0
    planned = work_list(ranking, progress, excluded)
    if not planned:
        ingest.log("nothing to do: every ranked artist is done, parked or excluded")
        for line in status_lines(ranking, progress, excluded):
            print(line)
        _write_result(args, EXIT_OK, "done", None, None)
        return EXIT_OK
    try:
        backends = real_backends(len(planned))
    except (ingest.IngestError, OSError, lastfm.LastfmError) as e:
        ingest.log(f"ERROR cannot start the session: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    problem = ingest.js_runtime_problem()
    if problem:
        ingest.log(f"WARN js runtime: {problem}; yt-dlp needs Deno >= {'.'.join(map(str, ingest.MIN_DENO))} for YouTube")
    progress.sessions += 1
    progress.ranking_computed_at = ranking["computed_at"]
    try:
        progress.save()
    except OSError as e:
        ingest.log(f"ERROR cannot write {PROGRESS_FILE} ({type(e).__name__}); not starting, nothing was done")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    if unparked:
        ingest.log(f"unparked {unparked} artist(s)")
    ingest.log(f"bootstrap session {progress.sessions}: {len(planned)} artist(s) to work, --max-new {args.max_new}, "
               f"--max-minutes {args.max_minutes}")
    state = ingest.RunState.load(directory / STATE_FILE)
    recorder = Recorder(progress)
    limits = make_limits(args.max_new, args.max_minutes)
    cfg = engine_config(ranking, section)
    lr, interrupted, error = run_engine(planned, cfg, backends, limits, state, recorder.on_artist, False, secrets_now())
    if interrupted:
        stop = "sign-in"
    else:
        stop = None if error else (limits.stop_reason or "done")
    ingest.log(ingest.lastfm_summary(lr))
    ingest.log(f"bootstrap session {progress.sessions}: {recorder.completed} artist(s) completed, {recorder.cut} cut, "
               f"{recorder.failed} enumeration(s) failed, {recorder.parked} newly parked")
    if interrupted:
        sign_in_stop()
    if error:
        ingest.log(f"ERROR the session stopped early: {error}")
    if recorder.unsaved or recorder.errors:
        ingest.log(f"ERROR progress is not fully saved ({recorder.errors} hook error(s), unsaved={recorder.unsaved}); "
                   f"the next session repeats what is missing")
    failed = bool(lr.failed or lr.counts.get("failed") or error or recorder.unsaved or recorder.errors)
    code = EXIT_SIGN_IN if interrupted else EXIT_FAILED if failed else EXIT_OK
    ingest.log(f"stop reason: {stop}")
    for line in status_lines(ranking, progress, excluded):
        ingest.log(line)
    artists = {"planned": len(planned), "completed": recorder.completed, "cut": recorder.cut,
               "failed": lr.failed, "not_started": lr.not_started, "parked": recorder.parked, "unusable": lr.unusable}
    _write_result(args, code, stop, result_counts(lr.counts, lr.not_attempted), artists)
    return code


def _write_result(args, code: int, stop: str | None, counts: dict | None, artists: dict | None) -> None:
    """--result-json: counts and words only (the wrapper reads the top-level `stop_reason`). Written for every run that
    got past the preconditions; a write failure is logged by ingest.write_result and changes nothing."""
    if not args.result_json:
        return
    ingest.write_result(args.result_json, {"stop_reason": stop, "exit_code": code,
                                           "counts": counts or result_counts({}), "artists": artists or {}})


# ---- covers (#250) ----

RETRY_DAYS_RANGE = bootstrap_covers.RETRY_DAYS_RANGE
COVERS_ENV = ["TG_BOT_TOKEN", "TG_CHANNEL_ID"] + D1_ENV  # no Spotify and no Last.fm: nothing of them is used


def cover_env(required: list[str]) -> str | None:
    """Loads ./.env (the real environment wins) and checks the variables: a message naming them, or None."""
    try:
        tg_check.load_env()
    except tg_check.EnvSyntaxError as e:
        return str(e)
    missing = [k for k in required if not os.environ.get(k)]
    return f"missing env vars: {', '.join(missing)}" if missing else None


def real_cover_backends() -> "bootstrap_covers.CoverBackends":
    return bootstrap_covers.real_backends()


def cover_query() -> Callable:
    """The D1 read function of `covers` (a function so tests can replace it)."""
    return ingest.d1_query


def cover_usage_problem(args) -> str | None:
    if args.count:
        if args.max_new is not None or args.max_minutes is not None or args.result_json:
            return "covers --count is read-only and takes no --max-new, --max-minutes or --result-json"
    elif args.max_new is None or args.max_minutes is None:
        return "covers needs both --max-new and --max-minutes (or --count for the dry count)"
    return None


def cmd_covers(args, lock_held: bool) -> int:
    problem = cover_usage_problem(args)
    if problem:
        refuse(problem)
        return EXIT_USAGE
    directory = state_dir()
    lock = None
    if not lock_held:  # the count takes the lock too: it must not read while a session or the 21:10 run writes
        try:
            lock = take_lock(directory)
        except Locked:
            refuse(f"another run or session holds {LOCK_FILE}; exiting without doing anything")
            return EXIT_LOCKED
        except OSError as e:
            refuse(f"cannot take the lock ({type(e).__name__})")
            return EXIT_USAGE
    try:
        return _covers_locked(args, directory)
    finally:
        if lock is not None:
            os.close(lock)


def real_artist_backends() -> "bootstrap_artist_photos.ArtistBackends":
    return bootstrap_artist_photos.real_backends()


def _artists_locked(args, directory: Path) -> int:
    """`covers --artists` (#253): the photo of artists stored without one. Same lock, 21:10 guard, caps, environment and
    exit codes as the album mode; the logic is bootstrap_artist_photos.py, the cooldown file artist-photo-backfill.json."""
    ap = bootstrap_artist_photos
    if not args.count:
        problem = overlap_problem(local_now(), args.max_minutes)
        if problem:
            refuse(problem)
            return EXIT_GUARD
    problem = cover_env(D1_ENV if args.count else COVERS_ENV)
    if problem:
        refuse(problem)
        return EXIT_USAGE
    try:
        state = ap.ArtistPhotoState.load(directory / ap.STATE_FILE)
    except bootstrap_covers.CoverStateError as e:
        refuse(f"state file unusable: {ap.STATE_FILE}: {e}; move it aside by hand to start empty")
        return EXIT_USAGE
    query = cover_query()
    try:
        if args.count:
            for line in ap.count_lines(query, state, today(), args.retry_days, args.retry_failed_days):
                print(line)
            return EXIT_OK
        todo, skipped = ap.read_todo(query)
    except (ingest.IngestError, OSError) as e:
        ingest.log(f"ERROR artist photos: cannot read D1: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    if todo is None:
        ingest.log("artist photos: nothing done, the artists.image_file_id column does not exist (migration 0003 not applied)")
        _write_result(args, EXIT_OK, "done", None, None)
        return EXIT_OK
    try:
        backends = real_artist_backends()
    except (ingest.IngestError, OSError, ImportError) as e:
        ingest.log(f"ERROR artist photos: cannot start: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    ingest.log(f"artist photos: {len(todo)} artist(s) without an image to work ({skipped['bad']} unreadable row(s) skipped), "
               f"--max-new {args.max_new}, --max-minutes {args.max_minutes}, retry cooldown {args.retry_days} days")
    limits = make_limits(args.max_new, args.max_minutes)
    run = ap.run_backfill(todo, backends, state, limits, today, args.retry_days, args.retry_failed_days)
    c = run.counts
    ingest.log("artist photos: " + ", ".join(f"{k} {v}" for k, v in c.items()) + f", cooldown {run.cooldown}, "
               f"not attempted {run.not_attempted}")
    ingest.log(f"artist photos: {len(todo) - c['photographed'] - c['already']} artist(s) still without an image")
    if run.interrupted:
        sign_in_stop()
    if run.unsaved:
        ingest.log(f"ERROR artist photos: {ap.STATE_FILE} is not fully saved; the next run repeats what is missing")
    code = EXIT_SIGN_IN if run.interrupted else EXIT_FAILED if run.failed else EXIT_OK
    ingest.log(f"stop reason: {run.stop_reason}")
    _write_result(args, code, run.stop_reason, {**c, "cooldown": run.cooldown, "not_attempted": run.not_attempted}, None)
    return code


def _covers_locked(args, directory: Path) -> int:
    if args.artists:
        return _artists_locked(args, directory)
    if not args.count:
        problem = overlap_problem(local_now(), args.max_minutes)
        if problem:
            refuse(problem)
            return EXIT_GUARD
    problem = cover_env(D1_ENV if args.count else COVERS_ENV)
    if problem:
        refuse(problem)
        return EXIT_USAGE
    try:
        state = bootstrap_covers.CoverState.load(directory / bootstrap_covers.STATE_FILE)
    except bootstrap_covers.CoverStateError as e:
        refuse(f"state file unusable: {bootstrap_covers.STATE_FILE}: {e}; move it aside by hand to start empty")
        return EXIT_USAGE
    query = cover_query()
    try:
        if args.count:
            for line in bootstrap_covers.count_lines(query, state, today(), args.retry_days, args.retry_failed_days):
                print(line)
            return EXIT_OK
        todo, skipped = bootstrap_covers.read_todo(query)
    except (ingest.IngestError, OSError) as e:
        ingest.log(f"ERROR covers: cannot read D1: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    try:
        backends = real_cover_backends()
    except (ingest.IngestError, OSError, ImportError) as e:
        ingest.log(f"ERROR covers: cannot start: {type(e).__name__}: {ingest.redact(str(e), secrets_now())}")
        _write_result(args, EXIT_FAILED, None, None, None)
        return EXIT_FAILED
    ingest.log(f"covers: {len(todo)} album(s) without a cover to work ({skipped['unknown']} unknown album skipped), "
               f"--max-new {args.max_new}, --max-minutes {args.max_minutes}, retry cooldown {args.retry_days} days")
    limits = make_limits(args.max_new, args.max_minutes)
    run = bootstrap_covers.run_backfill(todo, backends, state, limits, today, args.retry_days,
                                        args.retry_failed_days)
    c = run.counts
    ingest.log("covers: " + ", ".join(f"{k} {v}" for k, v in c.items()) + f", cooldown {run.cooldown}, "
               f"not attempted {run.not_attempted}")
    left = len(todo) - c["covered"] - c["already"]
    ingest.log(f"covers: {left} album(s) still without a cover")
    if run.interrupted:
        sign_in_stop()
    if run.unsaved:
        ingest.log(f"ERROR covers: {bootstrap_covers.STATE_FILE} is not fully saved; the next run repeats what is missing")
    code = EXIT_SIGN_IN if run.interrupted else EXIT_FAILED if run.failed else EXIT_OK
    ingest.log(f"stop reason: {run.stop_reason}")
    counts = {**c, "cooldown": run.cooldown, "not_attempted": run.not_attempted}
    _write_result(args, code, run.stop_reason, counts, None)
    return code


# ---- rank ----

def rank_client(key: str, years: int):
    """A client for the ranking: 1 + years * 53 + 4 requests (plus the minimum per-artist budget of the shared sizing
    function, which is never used: rank enumerates no artist). Never the default of 400, which is too small above
    about 7 years."""
    return ingest.lastfm_session_client(key, 1, ranking_calls=1 + years * lastfm.WEEKS_PER_YEAR_BOUND + 4,
                                        out=ingest.log)


def cmd_rank(args, lock_held: bool) -> int:
    try:
        cfg = load_config(args.config, required=True)
    except ConfigError as e:
        refuse(str(e))
        return EXIT_USAGE
    problem = prepare_env([])
    if problem:
        refuse(problem)
        return EXIT_USAGE
    directory = state_dir()
    lock = None
    if not lock_held:
        try:
            lock = take_lock(directory)
        except Locked:
            refuse(f"another run or session holds {LOCK_FILE}; exiting without doing anything")
            return EXIT_LOCKED
        except OSError as e:
            refuse(f"cannot take the lock ({type(e).__name__})")
            return EXIT_USAGE
    key = os.environ.get("LASTFM_API_KEY", "")
    try:
        client = rank_client(key, cfg.years)
        ranking = bootstrap_ranking.compute(client, cfg.user, cfg.years, cfg.max_artists, refresh=args.refresh,
                                            out=ingest.log)
    except bootstrap_ranking.RankingError as e:
        refuse(str(e))
        return EXIT_USAGE if e.kind == "exists" else EXIT_FAILED
    except (lastfm.LastfmError, ingest.IngestError, OSError) as e:
        text = ingest.redact(str(e), [key, cfg.user])
        ingest.log(f"ERROR cannot compute the ranking: {text}")
        return EXIT_USAGE if ingest.lastfm_config_problem(e) else EXIT_FAILED
    finally:
        if lock is not None:
            os.close(lock)
    ingest.log("next: status, plan, then run")
    return EXIT_OK


# ---- command line ----

def _caps(low_high: tuple[int, int]) -> Callable[[str], int]:
    low, high = low_high

    def parse(text: str) -> int:
        try:
            v = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a whole number") from None
        if not low <= v <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return v
    return parse


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bootstrap.py", allow_abbrev=False,
                                 description="Bootstrap the library from the stored Last.fm ranking (docs/bootstrap-ongoing.md).")
    sub = ap.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, caps: bool = False, config: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, allow_abbrev=False)
        if config:
            p.add_argument("--config", type=Path, metavar="FILE", default=None,
                           help="local config file with a lastfm_artists: section (default: "
                                "${XDG_CONFIG_HOME:-$HOME/.config}/cowlic/bootstrap.yml when it exists)")
        if caps:
            p.add_argument("--max-new", type=_caps(MAX_NEW_RANGE), required=True, metavar="N",
                           help="REQUIRED: stop after N tracks were attempted (1..1000000)")
            p.add_argument("--max-minutes", type=_caps(MAX_MINUTES_RANGE), required=True, metavar="M",
                           help="REQUIRED: start no new track after M minutes of wall clock (1..1440)")
        return p

    rank = command("rank", "compute and store the ranking (once)")
    rank.add_argument("--refresh", action="store_true", help="replace an existing ranking")
    command("plan", "dry run of the next session; writes nothing", caps=True)
    run = command("run", "one session", caps=True)
    run.add_argument("--result-json", type=Path, metavar="FILE", default=None,
                     help="write counts and the stop reason to FILE at the end (a repeated option: the last one wins)")
    run.add_argument("--reset-state", action="store_true",
                     help="move the bootstrap progress and engine state files aside to <name>.bak (numbered) and start empty")
    run.add_argument("--unpark", action="store_true", help="make every parked artist pending again (failures reset)")
    command("status", "progress and end state, no network")
    covers = command("covers", "backfill the covers of albums stored without one (no audio)", config=False)
    covers.add_argument("--count", action="store_true",
                        help="read-only dry count (D1 reads only): albums and artists without a cover, and the cooldown; "
                             "takes no caps")
    covers.add_argument("--artists", action="store_true",
                        help="(#253) work the artists stored without a photo instead of the albums without a cover "
                             "(state file artist-photo-backfill.json; same caps, lock, guard and exit codes)")
    covers.add_argument("--max-new", type=_caps(MAX_NEW_RANGE), default=None, metavar="N",
                        help="REQUIRED unless --count: stop after N albums were attempted (1..1000000)")
    covers.add_argument("--max-minutes", type=_caps(MAX_MINUTES_RANGE), default=None, metavar="M",
                        help="REQUIRED unless --count: start no new album after M minutes of wall clock (1..1440)")
    covers.add_argument("--retry-days", type=_caps(RETRY_DAYS_RANGE), default=bootstrap_covers.DEFAULT_RETRY_DAYS,
                        metavar="D", help="do not retry an album without a match or with a mismatch for D days "
                                          f"(0 = always retry; default {bootstrap_covers.DEFAULT_RETRY_DAYS})")
    covers.add_argument("--retry-failed-days", type=_caps(RETRY_DAYS_RANGE), default=bootstrap_covers.DEFAULT_FAILED_DAYS,
                        metavar="D", help="do not retry an album whose attempt failed (thumbnail, Telegram or search error) "
                                          f"for D days (0 = always retry; default {bootstrap_covers.DEFAULT_FAILED_DAYS})")
    covers.add_argument("--result-json", type=Path, metavar="FILE", default=None,
                        help="write counts and the stop reason to FILE at the end (a repeated option: the last one wins)")
    return ap


def main(argv: list[str] | None = None) -> int:
    lock_held = os.environ.get("COWLIC_LOCK_HELD") == "1"  # the REAL environment, before any .env load; .env is ignored
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse: 2 for a usage error, 0 for --help
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if args.command == "status":
        return cmd_status(args)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "run":
        return cmd_run(args, lock_held)
    if args.command == "covers":
        return cmd_covers(args, lock_held)
    return cmd_rank(args, lock_held)


if __name__ == "__main__":
    sys.exit(main())

"""Match-rate report on a sample of artists: the go/no-go gate for the artist ingest (#118, umbrella #102).

Usage:
  python ingest/match_report.py <artist id or URL> [<artist id or URL> ...] [--artists-file FILE]
         [--max-tracks 150] [--min-interval 1.0] [--out match-report-<UTC time>.csv]

Flow per artist: ingest/artist_tracks.py (Spotify catalog, Client Credentials) -> ingest/dedupe.py (collapse) ->
a deterministic sample -> match.find_match through ytmusicapi (the same call the pipeline makes) for every sampled
track. Nothing is downloaded or uploaded; no Telegram, no D1, no Spotify user login, no cookies. The only network
traffic is Spotify catalog reads and YouTube Music searches.

Output: a LOCAL CSV (gitignored: the name must match match-report*.csv) with one row per sampled track, and a summary
on stdout: counts and the match rate overall and per artist, plus the lowest-similarity rows for eyeballing.
Reading the result and the thresholds: docs/local-ingest.md, "Match-rate report".

The YouTube Music search is throttled: at least --min-interval seconds (>= 1) between two searches, bounded
backoff on HTTP 429/5xx (ingest.Throttle), and hard caps: --max-tracks tracks per run (so at most 3 searches per
track, 4 x max-tracks searches counting retries), and a stop after 3 failed tracks in a row.

Rows: status `matched` (find_match accepted a result), `no_match` (no result passed title >= 0.85 and
duration +-3 s; the columns then show the closest result YouTube Music returned, and `reason` says whether title or
duration rejected it), `uncomparable` (the track has no usable title or duration; no search was made), `error` (the
search failed after retries). The match rate is matched / (matched + no_match).
"""
import argparse
import csv
import datetime
import hashlib
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import tg_check
from artist_tracks import ArtistTracksError, Catalog, artist_tracks, client_credentials_token, parse_artist
from dedupe import collapse
from ingest import Throttle, primary_artist, redact, transient_search
from match import TITLE_MIN_SIMILARITY, find_match, similarity

DEFAULT_MAX_TRACKS = 150
MAX_TRACKS_LIMIT = 1000
MAX_ARTISTS = 50
MIN_INTERVAL_FLOOR = 1.0  # seconds; the CLI refuses a faster pace
SEARCHES_PER_TRACK_BOUND = 4  # find_match makes at most 3 searches per track; one spare for a retry
MAX_CONSECUTIVE_ERRORS = 3
WORST_ROWS = 15
TEXT_MAX = 200
ARTISTS_FILE_MAX_BYTES = 64 * 1024
GO_RATE, TUNE_RATE = 0.85, 0.60  # >= GO_RATE go; >= TUNE_RATE tune the matcher first; below that no-go
MIN_SAMPLE = 30  # fewer comparable tracks than this: the verdict is only a hint
COLUMNS = ["artist", "artist_id", "title", "duration_s", "matched_title", "matched_duration_s", "duration_delta_s",
           "similarity", "status", "reason"]
STATUSES = ("matched", "no_match", "uncomparable", "error")
_REPORT_NAME = re.compile(r"match-report[A-Za-z0-9._-]*\.csv")


class ReportError(Exception):
    """Invalid input or an unusable output path; the message is safe to print."""


class SearchBudgetExceeded(Exception):
    """The hard cap on YouTube Music searches of this run was reached."""


# ---- text hygiene ----

def clean_text(value) -> str:
    """Untrusted text for a table cell or a terminal line: control and format characters removed (whitespace
    controls become a space), length bounded. Never raises."""
    if not isinstance(value, str):
        return ""
    out = []
    for c in value[:TEXT_MAX * 2]:
        cat = unicodedata.category(c)
        if c in "\t\r\n\x0b\x0c" or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat[0] != "C":
            out.append(c)
    return "".join(out).strip()[:TEXT_MAX]


def csv_cell(value) -> str:
    """A text cell safe to open in a spreadsheet: cleaned, and a leading = + - @ gets a leading apostrophe."""
    text = clean_text(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _num(value: float | None, digits: int = 1) -> str:
    return "" if value is None else f"{value:.{digits}f}"


# ---- input ----

def read_artists_file(path: str) -> list[str]:
    try:
        p = Path(path)
        if p.stat().st_size > ARTISTS_FILE_MAX_BYTES:
            raise ReportError(f"artists file is larger than {ARTISTS_FILE_MAX_BYTES} bytes")
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ReportError(f"cannot read the artists file ({type(e).__name__})") from None
    values = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                values.append(parse_artist(line))
            except ArtistTracksError as e:
                raise ReportError(f"artists file line {n}: {e}") from None
    return values


def collect_artists(args_artists: list[str], file_path: str | None) -> list[str]:
    """Validated artist ids in the order given (command line first, then the file), without repeats."""
    ids = [parse_artist(a) for a in args_artists]
    if file_path:
        ids += read_artists_file(file_path)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ReportError("give at least one artist id or URL (arguments or --artists-file)")
    if len(ids) > MAX_ARTISTS:
        raise ReportError(f"at most {MAX_ARTISTS} artists per run")
    return ids


def validate_out(path: str) -> Path:
    p = Path(path)
    if not _REPORT_NAME.fullmatch(p.name):
        raise ReportError("the output file name must look like match-report*.csv (that pattern is gitignored)")
    if p.exists():
        raise ReportError("the output file already exists; refusing to overwrite it")
    if not p.parent.is_dir():
        raise ReportError("the output directory does not exist")
    return p


# ---- the search (paced, bounded, recorded) ----

class PacedSearch:
    """Wraps a YTMusic-like object: spaces the searches, retries transient errors with bounded backoff, enforces a
    hard budget, and records the results of the current track so a rejected one can be shown in the report."""

    def __init__(self, ytm, throttle: Throttle, min_interval: float, max_searches: int,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._ytm, self._throttle, self._interval, self._max = ytm, throttle, min_interval, max_searches
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None
        self.searches = 0
        self.results: list[dict] = []

    def begin_track(self) -> None:
        self.results = []

    def search(self, query, **kwargs) -> list:
        def once():
            if self.searches >= self._max:
                raise SearchBudgetExceeded(f"search budget of {self._max} reached")
            if self._last is not None:
                wait = self._interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self.searches += 1
            try:
                return self._ytm.search(query, **kwargs)
            finally:
                self._last = self._clock()

        found = self._throttle.call(once, transient_search, "YouTube Music search")
        self.results.extend(r for r in found if isinstance(r, dict))
        return found


# ---- classification ----

def usable_duration(song: dict) -> float | None:
    d = song.get("duration")
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d <= 0:
        return None
    return float(d)


def _result_duration(r: dict) -> float | None:
    d = r.get("duration_seconds")
    return float(d) if isinstance(d, (int, float)) and not isinstance(d, bool) and math.isfinite(d) and d > 0 else None


def closest_result(title: str, duration: float, results: list[dict]) -> dict | None:
    """The returned result whose title is most similar (ties: nearest duration), whatever its duration."""
    best, best_key = None, None
    for r in results:
        rt = r.get("title")
        if not isinstance(rt, str):
            continue
        rd = _result_duration(r)
        key = (similarity(title, rt), -(abs(rd - duration) if rd is not None else 1e9))
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


@dataclass
class Row:
    artist: str
    artist_id: str
    title: str
    duration: float | None
    status: str
    matched_title: str = ""
    matched_duration: float | None = None
    delta: float | None = None
    similarity: float | None = None
    reason: str = ""

    def cells(self) -> list[str]:
        return [csv_cell(self.artist), self.artist_id, csv_cell(self.title), _num(self.duration),
                csv_cell(self.matched_title), _num(self.matched_duration), _num(self.delta), _num(self.similarity, 3),
                self.status, csv_cell(self.reason)]


def classify(song: dict, artist_id: str, ps: PacedSearch, find: Callable = find_match) -> Row:
    """One row for one song record. Raises on a failed search (the caller turns that into status `error`)."""
    name = song.get("name") if isinstance(song.get("name"), str) else ""
    artist = primary_artist(song)
    dur = usable_duration(song)
    if not clean_text(name) or dur is None:
        return Row(artist, artist_id, name, dur, "uncomparable", reason="no usable title or duration")
    ps.begin_track()
    m = find(ps, name, artist, song.get("album_name"), int(dur))
    if m is not None:
        return Row(artist, artist_id, name, dur, "matched", m.title, float(m.duration_s), m.duration_s - dur,
                   similarity(name, m.title))
    near = closest_result(name, dur, ps.results)
    if near is None:
        return Row(artist, artist_id, name, dur, "no_match", reason="no_results")
    sim = similarity(name, near["title"])
    nd = _result_duration(near)
    return Row(artist, artist_id, name, dur, "no_match", near["title"], nd, None if nd is None else nd - dur, sim,
               "duration" if sim >= TITLE_MIN_SIMILARITY else "title")


# ---- the run ----

@dataclass
class ArtistOutcome:
    artist_id: str
    name: str = ""
    listed: int = 0
    unique: int = 0
    sampled: int = 0
    error: str | None = None
    counts: dict = field(default_factory=lambda: dict.fromkeys(STATUSES, 0))


def sample(songs: list[dict], quota: int) -> list[dict]:
    """A deterministic, unbiased sample: ordered by a hash of the Spotify id, the first `quota` taken, then sorted
    by title for the output."""
    ranked = sorted(songs, key=lambda s: hashlib.sha256(str(s.get("song_id")).encode("utf-8")).hexdigest())[:quota]
    return sorted(ranked, key=lambda s: (str(s.get("name")).casefold(), str(s.get("song_id"))))


def _artist_label(songs: list[dict], artist_id: str) -> str:
    names = [s.get("album_artist") for s in songs if isinstance(s.get("album_artist"), str) and s["album_artist"]]
    return clean_text(max(sorted(set(names)), key=names.count)) if names else artist_id


def run(artist_ids: list[str], enumerate_songs: Callable[[str], list[dict]], ps: PacedSearch, max_tracks: int,
        rows: list[Row], secrets: list[str], out: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
        find: Callable = find_match) -> tuple[list[ArtistOutcome], str | None]:
    """Appends the rows to `rows` as they are produced (so a partial result survives an interrupt). Returns the
    per-artist outcomes and a stop reason (None when everything ran). An error in one artist does not stop the
    others; a search budget hit or too many failed tracks in a row stops the run."""
    if max_tracks < len(artist_ids):
        raise ReportError("--max-tracks must be at least the number of artists")
    outcomes = [ArtistOutcome(a) for a in artist_ids]
    remaining, consecutive = max_tracks, 0
    for i, oc in enumerate(outcomes):
        quota = -(-remaining // (len(outcomes) - i))  # ceil: unused budget flows to the later artists
        try:
            songs = enumerate_songs(oc.artist_id)
            oc.listed = len(songs)
            oc.name = _artist_label(songs, oc.artist_id)
            kept = collapse(songs).kept
            oc.unique = len(kept)
            chosen = sample(kept, quota)
        except Exception as e:  # one artist failing must not abort the others; reported, never swallowed
            oc.error = f"{type(e).__name__}: {redact(str(e), secrets)}"[:300]
            out(f"ERROR artist {oc.artist_id}: {clean_text(oc.error)}")
            continue
        remaining -= len(chosen)
        out(f"{oc.name or oc.artist_id}: {oc.listed} listed, {oc.unique} unique, sampling {len(chosen)}")
        for song in chosen:
            try:
                row = classify(song, oc.artist_id, ps, find)
                consecutive = 0
            except SearchBudgetExceeded:
                return outcomes, "search budget reached"
            except Exception as e:  # a failed search is a data point (status error), not a crash
                consecutive += 1
                out(f"WARN search failed ({type(e).__name__}): {clean_text(redact(str(e), secrets))[:200]}")
                row = Row(primary_artist(song), oc.artist_id, str(song.get("name") or ""), usable_duration(song),
                          "error", reason=type(e).__name__)
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    _record(oc, row, rows)
                    return outcomes, f"{MAX_CONSECUTIVE_ERRORS} searches failed in a row (YouTube may be limiting us)"
            _record(oc, row, rows)
    return outcomes, None


def _record(oc: ArtistOutcome, row: Row, rows: list[Row]) -> None:
    rows.append(row)
    oc.sampled += 1
    oc.counts[row.status] += 1


# ---- output ----

def write_csv(path: Path, rows: list[Row]) -> None:
    """New file only (never overwrites), mode 0600, UTF-8 with BOM so a spreadsheet reads non-Latin titles."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow(r.cells())


def rate(counts: dict) -> float | None:
    done = counts["matched"] + counts["no_match"]
    return counts["matched"] / done if done else None


def _pct(r: float | None) -> str:
    return "n/a" if r is None else f"{r * 100:.1f}%"


def verdict(r: float | None, comparable: int) -> str:
    if r is None:
        return "NO VERDICT: nothing was compared"
    word = ("GO" if r >= GO_RATE else "TUNE THE MATCHER FIRST" if r >= TUNE_RATE else "NO-GO")
    hint = f" (indicative only: fewer than {MIN_SAMPLE} comparable tracks)" if comparable < MIN_SAMPLE else ""
    return f"{word}{hint}"


def worst_rows(rows: list[Row], n: int = WORST_ROWS) -> list[Row]:
    """The failures with the lowest similarity first (no_match rows only; a row with no similarity is lowest)."""
    bad = [r for r in rows if r.status == "no_match"]
    return sorted(bad, key=lambda r: (-1.0 if r.similarity is None else r.similarity, r.artist.casefold(),
                                      r.title.casefold()))[:n]


def summary_lines(rows: list[Row], outcomes: list[ArtistOutcome], stop: str | None, csv_path: Path | None) -> list[str]:
    total = dict.fromkeys(STATUSES, 0)
    for r in rows:
        total[r.status] += 1
    comparable = total["matched"] + total["no_match"]
    lines = ["", "== Match-rate report ==",
             f"tracks sampled: {len(rows)}  matched: {total['matched']}  no_match: {total['no_match']}  "
             f"uncomparable: {total['uncomparable']}  error: {total['error']}",
             f"match rate (matched / (matched + no_match)): {_pct(rate(total))}  [{comparable} compared]",
             f"verdict: {verdict(rate(total), comparable)}  "
             f"(>= {GO_RATE:.0%} go, {TUNE_RATE:.0%}-{GO_RATE:.0%} tune first, < {TUNE_RATE:.0%} no-go)"]
    if stop:
        lines.append(f"STOPPED EARLY: {stop}; the numbers cover only what ran")
    lines.append("per artist:")
    for oc in outcomes:
        label = f"{oc.name or oc.artist_id} ({oc.artist_id})"
        if oc.error:
            lines.append(f"  {label}: FAILED {clean_text(oc.error)}")
        else:
            c = oc.counts
            lines.append(f"  {label}: {oc.sampled} sampled of {oc.unique} unique ({oc.listed} listed)  "
                         f"matched {c['matched']}  no_match {c['no_match']}  uncomparable {c['uncomparable']}  "
                         f"error {c['error']}  rate {_pct(rate(c))}")
    bad = worst_rows(rows)
    if bad:
        lines.append("lowest-similarity failures (eyeball these; a wrong reason here means the matcher needs work):")
        for r in bad:
            lines.append(f"  {_num(r.similarity, 3) or '  -  '}  {r.reason:<10} {clean_text(r.artist)} - "
                         f"{clean_text(r.title)} ({_num(r.duration)} s)  closest: "
                         f"{clean_text(r.matched_title) or '-'} ({_num(r.matched_duration) or '-'} s)")
    if csv_path:
        lines.append(f"CSV: {csv_path}")
    return lines


# ---- CLI ----

def _bounded_int(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= n <= MAX_TRACKS_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_TRACKS_LIMIT}")
    return n


def _interval(text: str) -> float:
    try:
        x = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(x) or not MIN_INTERVAL_FLOOR <= x <= 60:
        raise argparse.ArgumentTypeError(f"must be between {MIN_INTERVAL_FLOOR:g} and 60 seconds")
    return x


def default_out() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("match-report-%Y%m%dT%H%M%SZ.csv")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Match-rate report on a sample of Spotify artists (no download).")
    ap.add_argument("artists", nargs="*", help="artist id or https://open.spotify.com/artist/<id> URL")
    ap.add_argument("--artists-file", help="text file, one artist id or URL per line (# comments allowed)")
    ap.add_argument("--max-tracks", type=_bounded_int, default=DEFAULT_MAX_TRACKS,
                    help=f"hard cap on tracks searched in this run (default {DEFAULT_MAX_TRACKS})")
    ap.add_argument("--min-interval", type=_interval, default=MIN_INTERVAL_FLOOR,
                    help=f"seconds between two YouTube Music searches (default and minimum {MIN_INTERVAL_FLOOR:g})")
    ap.add_argument("--out", default=None, help="CSV path, must be named match-report*.csv (default: in the cwd)")
    args = ap.parse_args(argv)
    secrets: list[str] = []
    rows: list[Row] = []
    outcomes: list[ArtistOutcome] = []
    stop = None
    try:
        ids = collect_artists(args.artists, args.artists_file)
        if args.max_tracks < len(ids):
            raise ReportError("--max-tracks must be at least the number of artists")
        out_path = validate_out(args.out or default_out())
        tg_check.load_env()
        cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not cid or not secret:
            raise ReportError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
        secrets = [cid, secret]
        from ytmusicapi import YTMusic  # imported late: the pure parts and the tests need no network library
        cat = Catalog(lambda: client_credentials_token(cid, secret), secrets)
        ps = PacedSearch(YTMusic(), Throttle(), args.min_interval, SEARCHES_PER_TRACK_BOUND * args.max_tracks)
        try:
            outcomes, stop = run(ids, lambda a: artist_tracks(cat, a, out=lambda s: None), ps, args.max_tracks, rows,
                                 secrets)
        finally:
            write_csv(out_path, rows)  # also after an interrupt: what was learned is kept
    except (ReportError, ArtistTracksError, tg_check.EnvSyntaxError, OSError) as e:
        print(f"ERROR {redact(str(e), secrets)}", file=sys.stderr)
        return 1
    for line in summary_lines(rows, outcomes, stop, out_path):
        print(line)
    failed = stop is not None or any(o.error for o in outcomes)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

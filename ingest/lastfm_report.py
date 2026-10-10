"""Match-rate / dry-run report for the Last.fm artist source: the numbers behind the go / no-go before any real
download (#153, umbrella #102). Sibling of match_report.py (the Spotify one, #118): same CSV conventions, same
throttle, same gate (#92: matched >= 80% AND wrong <= 2%).

Usage:
  python ingest/lastfm_report.py --lastfm-user <name> [--years 5] [--artists 100] [--sample-artists 10]
         [--max-tracks 150] [--max-minutes 45] [--min-interval 1.0] [--check-stored] [--out match-report-lastfm-NAME.csv]
  python ingest/lastfm_report.py --summarize match-report-lastfm-NAME.csv      (offline: re-read a labelled CSV)

Flow: rank the top --artists artists of the user over --years years (lastfm.top_artists: about 53 weekly-chart calls
per year, 1 call per second) -> pick --sample-artists of them, evenly spaced over the ranking -> enumerate their tracks
(lastfm_tracks.artist_tracks, the most popular albums only) -> collapse (dedupe.py) -> a deterministic sample ->
match.find_match through ytmusicapi (the same call the pipeline makes) for every sampled track, in the mode the
pipeline would use: with the Last.fm duration (+-3 s) or, when Last.fm gives none, by title and artist only (#151).
Nothing is downloaded or uploaded; no Telegram, no D1 write, no cookies, no Spotify. The only network traffic is
read-only Last.fm calls and YouTube Music searches, plus (only with --check-stored) one read-only D1 SELECT.

Secrets: LASTFM_API_KEY from the environment (.env, loaded the way the other ingest scripts do); it is never
printed and never written to the CSV. The Last.fm user name is not printed either. Titles and names are untrusted
text (cleaned, and a CSV cell starting with = + - @ gets a leading apostrophe).

Hard caps: --max-tracks tracks (so at most 4 x that many YouTube searches), --max-minutes of wall time (checked
between tracks and artists), a Last.fm request budget (ranking bound + a small budget per sampled artist), >= 1 s
between two searches, and a stop after 3 failed searches (or 3 failed artists) in a row.

Rows: status `matched`, `no_match` (the columns then show the closest YouTube result), `stored` (already in D1 under
another id; only with --check-stored; not searched), `error` (the search failed). `mode` is the matcher mode the
track was tried in: `duration` or `title-only`. The owner fills `reviewed` with `ok` or `wrong` on matched rows; the
summary (also --summarize) then gives the wrong rate overall and per mode. Reading it: docs/local-ingest.md,
"Last.fm match-rate report".
"""
import argparse
import csv
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import dedupe
import lastfm
import lastfm_tracks
import match_report as mr
import tg_check
from ingest import Throttle, YouTubeSignInRequired, is_sign_in_challenge, primary_artist, read_stored_rows, redact
from match import MODE_DURATION, MODE_TITLE_ONLY, TITLE_MIN_SIMILARITY, TITLE_ONLY_TITLE_MIN_SIMILARITY, find_match, similarity
from match_report import (ReportError, SearchBudgetExceeded, clean_text, csv_cell, _num, _pct)

DEFAULT_YEARS = 5
DEFAULT_ARTISTS = 100  # size of the ranking
DEFAULT_SAMPLE_ARTISTS = 10
MAX_SAMPLE_ARTISTS = 50
DEFAULT_MAX_MINUTES = 45
MAX_MINUTES_LIMIT = 240
ALBUMS_PER_ARTIST = 20  # the report reads the most popular albums only (a real run reads up to 100)
CALL_BUDGET_PER_ARTIST = 30  # Last.fm requests per sampled artist, retries included (1 page + 20 albums + spare)
STATUSES = ("matched", "no_match", "stored", "error")
MODES = (MODE_DURATION, MODE_TITLE_ONLY)
LABEL_OK, LABEL_WRONG = "ok", "wrong"
LOWEST_TITLE_ONLY = 15
CSV_MAX_BYTES = 5 * 1024 * 1024
CSV_MAX_ROWS = 2000
COLUMNS = ["artist_rank", "artist", "title", "lastfm_duration_s", "mode", "matched_title", "matched_artists",
           "matched_duration_s", "duration_delta_s", "similarity", "status", "reason", "pattern", "reviewed"]


def ranking_call_bound(years: int) -> int:
    """Last.fm requests the ranking can use: 1 chart list + the weekly charts (the same bound ingest.py uses)."""
    return 1 + years * lastfm.WEEKS_PER_YEAR_BOUND + 4


# ---- rows ----

@dataclass
class Row:
    rank: int
    artist: str
    title: str
    duration: int | None  # the Last.fm duration; None: matched by title and artist only
    mode: str
    status: str
    matched_title: str = ""
    matched_artists: str = ""
    matched_duration: float | None = None
    delta: float | None = None
    similarity: float | None = None
    reason: str = ""
    pattern: str = ""  # failing pattern, only for no_match rows (match_report.failure_pattern)
    reviewed: str = ""  # the owner's label: ok / wrong (never written by the report itself)

    def cells(self) -> list[str]:
        return [str(self.rank), csv_cell(self.artist), csv_cell(self.title), _num(self.duration, 0), self.mode,
                csv_cell(self.matched_title), csv_cell(self.matched_artists), _num(self.matched_duration),
                _num(self.delta), _num(self.similarity, 3), self.status, csv_cell(self.reason), self.pattern,
                csv_cell(self.reviewed)]


def _closest_by_title(title: str, results: list[dict]) -> dict | None:
    best, best_sim = None, -1.0
    for r in results:
        if isinstance(r.get("title"), str):
            s = similarity(title, r["title"])
            if s > best_sim:
                best, best_sim = r, s
    return best


def classify(rec: dict, rank: int, ps: mr.PacedSearch, find: Callable = find_match) -> Row:
    """One row for one Last.fm record. Raises on a failed search (the caller turns that into status `error`)."""
    name, artist, dur = rec["name"], primary_artist(rec), rec.get("duration")
    dur = dur if isinstance(dur, int) and not isinstance(dur, bool) and dur > 0 else None
    mode = MODE_DURATION if dur else MODE_TITLE_ONLY
    ps.begin_track()
    m = find(ps, name, artist, rec.get("album_name"), dur)
    if m is not None:
        return Row(rank, artist, name, dur, getattr(m, "mode", mode), "matched", m.title, m.artists,
                   float(m.duration_s), None if dur is None else m.duration_s - dur, similarity(name, m.title))
    near = mr.closest_result(name, float(dur), ps.results) if dur else _closest_by_title(name, ps.results)
    if near is None:
        return Row(rank, artist, name, dur, mode, "no_match", reason="no_results",
                   pattern=mr.failure_pattern(name, "no_results"))
    sim = similarity(name, near["title"])
    nd = mr._result_duration(near)
    if dur:
        reason = "duration" if sim >= TITLE_MIN_SIMILARITY else "title"
    else:  # title-only: the title was found but the artist, a variant word or the length rejected it
        reason = "artist_variant_or_length" if sim >= TITLE_ONLY_TITLE_MIN_SIMILARITY else "title"
    return Row(rank, artist, name, dur, mode, "no_match", near["title"],
               ", ".join(a["name"] for a in near.get("artists") or [] if isinstance(a, dict)
                         and isinstance(a.get("name"), str)),
               nd, None if nd is None or not dur else nd - dur, sim, reason, mr.failure_pattern(name, reason))


# ---- the run ----

@dataclass
class ArtistOutcome:
    rank: int
    name: str
    listed: int = 0  # records Last.fm gave
    unique: int = 0  # after the collapse of duplicate recordings
    unique_no_duration: int = 0
    sampled: int = 0
    error: str | None = None
    counts: dict = field(default_factory=lambda: dict.fromkeys(STATUSES, 0))


@dataclass
class RunInfo:
    ranked: int = 0
    last_calls: int = 0
    last_budget: int = 0
    searches: int = 0
    search_budget: int = 0
    stored_note: str = ""  # one line on the stored check: what it did, or why it was skipped


def pick_artists(ranking: list[dict], n: int) -> list[tuple[int, dict]]:
    """(rank, artist) for n artists evenly spaced over the ranking (rank 1 first); deterministic."""
    n = min(n, len(ranking))
    idx = sorted({i * len(ranking) // n for i in range(n)}) if n else []
    return [(i + 1, ranking[i]) for i in idx]


def run(picked: list[tuple[int, dict]], enumerate_songs: Callable[[dict], list[dict]], ps: mr.PacedSearch,
        max_tracks: int, rows: list[Row], secrets: list[str], stored_rows: list[tuple] | None = None,
        deadline: float | None = None, clock: Callable[[], float] = time.monotonic,
        out: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
        find: Callable = find_match) -> tuple[list[ArtistOutcome], str | None]:
    """Appends rows to `rows` as they are produced (a partial result survives an interrupt). Returns the per-artist
    outcomes and a stop reason (None when everything ran). One artist failing does not stop the others; the search
    budget, the time cap, a YouTube sign-in challenge, 3 failed searches or 3 failed artists in a row do."""
    if max_tracks < len(picked):
        raise ReportError("--max-tracks must be at least the number of sampled artists")
    outcomes = [ArtistOutcome(rank, a["name"]) for rank, a in picked]
    remaining, failed_tracks, failed_artists = max_tracks, 0, 0
    for i, (oc, (_, artist)) in enumerate(zip(outcomes, picked)):
        if deadline is not None and clock() >= deadline:
            return outcomes, "time cap reached"
        quota = -(-remaining // (len(outcomes) - i))  # ceil: unused budget flows to the later artists
        try:
            songs = enumerate_songs(artist)
            kept = dedupe.collapse(songs).kept
            oc.listed, oc.unique = len(songs), len(kept)
            oc.unique_no_duration = sum(1 for s in kept if not s.get("duration"))
            chosen = mr.sample(kept, quota)
            failed_artists = 0
        except Exception as e:  # reported, never swallowed
            oc.error = f"{type(e).__name__}: {redact(str(e), secrets)}"[:300]
            out(f"ERROR artist rank {oc.rank}: {clean_text(oc.error)}")
            failed_artists += 1
            if failed_artists >= mr.MAX_CONSECUTIVE_ERRORS:
                return outcomes, f"{failed_artists} artists failed in a row (Last.fm refuses or is down)"
            continue
        remaining -= len(chosen)
        already = set()
        if stored_rows is not None and chosen:
            fresh = {id(r) for r in dedupe.drop_stored(chosen, stored_rows).new}
            already = {id(r) for r in chosen} - fresh
        out(f"rank {oc.rank} {clean_text(oc.name)}: {oc.listed} listed, {oc.unique} unique, sampling {len(chosen)}")
        for song in chosen:
            if deadline is not None and clock() >= deadline:
                return outcomes, "time cap reached"
            if id(song) in already:
                dur = song.get("duration")
                _record(oc, Row(oc.rank, oc.name, song["name"], dur, MODE_DURATION if dur else MODE_TITLE_ONLY,
                                "stored", reason="already stored under another id"), rows)
                continue
            try:
                row = classify(song, oc.rank, ps, find)
                failed_tracks = 0
            except SearchBudgetExceeded:
                return outcomes, "search budget reached"
            except Exception as e:  # a failed search is a data point (status error), not a crash
                failed_tracks += 1
                dur = song.get("duration")
                row = Row(oc.rank, oc.name, str(song.get("name") or ""), dur, MODE_DURATION if dur else MODE_TITLE_ONLY,
                          "error", reason=type(e).__name__)
                if isinstance(e, YouTubeSignInRequired) or is_sign_in_challenge([str(e)]):
                    _record(oc, row, rows)
                    return outcomes, "YouTube asks to sign in (bot check); not bypassed, try again later"
                out(f"WARN search failed ({type(e).__name__}): {clean_text(redact(str(e), secrets))[:200]}")
                if failed_tracks >= mr.MAX_CONSECUTIVE_ERRORS:
                    _record(oc, row, rows)
                    return outcomes, f"{mr.MAX_CONSECUTIVE_ERRORS} searches failed in a row (YouTube may be limiting us)"
            _record(oc, row, rows)
    return outcomes, None


def _record(oc: ArtistOutcome, row: Row, rows: list[Row]) -> None:
    rows.append(row)
    oc.sampled += 1
    oc.counts[row.status] += 1


# ---- reading a labelled CSV back ----

def _opt_float(text: str) -> float | None:
    try:
        x = float(text)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def read_rows(path: str) -> list[Row]:
    """Rows of a CSV this script wrote (the owner may have filled `reviewed`). Bounded; a file that does not look
    like one is refused, never guessed at."""
    try:
        p = Path(path)
        if p.stat().st_size > CSV_MAX_BYTES:
            raise ReportError(f"the CSV is larger than {CSV_MAX_BYTES} bytes")
        text = p.read_text(encoding="utf-8-sig", newline="")
    except (OSError, UnicodeDecodeError) as e:
        raise ReportError(f"cannot read the CSV ({type(e).__name__})") from None
    try:
        table = list(csv.reader(text.splitlines()))
    except csv.Error:
        raise ReportError("the file is not a valid CSV") from None
    if not table or table[0] != COLUMNS:
        raise ReportError("not a Last.fm match report CSV (the header differs from the one lastfm_report.py writes)")
    if len(table) - 1 > CSV_MAX_ROWS:
        raise ReportError(f"more than {CSV_MAX_ROWS} rows")
    rows = []
    for n, c in enumerate(table[1:], 2):
        if len(c) != len(COLUMNS):
            raise ReportError(f"CSV line {n}: expected {len(COLUMNS)} columns")
        rank = int(c[0]) if c[0].isascii() and c[0].isdigit() and len(c[0]) <= 6 else None
        if rank is None or c[10] not in STATUSES or c[4] not in MODES:
            raise ReportError(f"CSV line {n}: invalid rank, mode or status")
        dur = _opt_float(c[3])
        rows.append(Row(rank, clean_text(c[1]), clean_text(c[2]), None if dur is None else int(dur), c[4], c[10],
                        clean_text(c[5]), clean_text(c[6]), _opt_float(c[7]), _opt_float(c[8]), _opt_float(c[9]),
                        clean_text(c[11]), c[12][:40], c[13].strip().casefold()[:20]))
    return rows


# ---- numbers ----

def counts_of(rows: list[Row], mode: str | None = None) -> dict:
    c = dict.fromkeys(STATUSES, 0)
    for r in rows:
        if mode is None or r.mode == mode:
            c[r.status] += 1
    return c


def rate(c: dict) -> float | None:
    done = c["matched"] + c["no_match"]
    return c["matched"] / done if done else None


def wrong_stats(rows: list[Row], mode: str | None = None) -> dict:
    """Labels of the matched rows: ok, wrong, unrecognised (a label that is neither), unlabelled, matched."""
    s = dict.fromkeys(("ok", "wrong", "other", "unlabelled", "matched"), 0)
    for r in rows:
        if r.status == "matched" and (mode is None or r.mode == mode):
            s["matched"] += 1
            lab = r.reviewed
            s["ok" if lab == LABEL_OK else "wrong" if lab == LABEL_WRONG else "other" if lab else "unlabelled"] += 1
    return s


def wrong_rate(s: dict) -> float | None:
    checked = s["ok"] + s["wrong"]
    return s["wrong"] / checked if checked else None


def verdict(rows: list[Row]) -> str:
    """The #92 gate. Go needs the matched rate >= 80% AND every mode that has matched rows labelled with a wrong
    rate <= 2% (the title-only mode is judged on its own rate), plus the overall wrong rate <= 2%."""
    r = rate(counts_of(rows))
    if r is None:
        return "no verdict (nothing was compared)"
    if r < mr.GO_RATE:
        return "matched rate < 80%: no-go for now (tune the matcher; see the failing patterns and the per-mode rates)"
    missing = [m for m in MODES if (s := wrong_stats(rows, m))["matched"] and not s["ok"] + s["wrong"]]
    if missing:
        return (f"matched rate >= 80%, but no wrong-rate label for mode {', '.join(missing)}: not a go yet "
                "(label matched rows ok/wrong in `reviewed`, then --summarize the CSV)")
    bad = [name for name, s in [("overall", wrong_stats(rows))] + [(m, wrong_stats(rows, m)) for m in MODES]
           if (wrong_rate(s) or 0.0) > mr.MAX_WRONG_RATE]
    if bad:
        return f"matched rate >= 80% but the wrong rate is above 2% ({', '.join(bad)}): no-go for now"
    return "matched rate >= 80% and every labelled wrong rate <= 2%: go (judge how large the labelled samples are)"


def lowest_title_only(rows: list[Row], n: int = LOWEST_TITLE_ONLY) -> list[Row]:
    """Title-only matches with the lowest title similarity first: the ones to eyeball for a wrong recording."""
    ms = [r for r in rows if r.status == "matched" and r.mode == MODE_TITLE_ONLY]
    return sorted(ms, key=lambda r: (1.0 if r.similarity is None else r.similarity, r.artist.casefold(),
                                     r.title.casefold()))[:n]


def result_lines(rows: list[Row]) -> list[str]:
    total = counts_of(rows)
    comparable = total["matched"] + total["no_match"]
    searched = comparable + total["error"]
    lines = [f"tracks sampled: {len(rows)}  matched: {total['matched']}  no_match: {total['no_match']}  "
             f"stored: {total['stored']}  error: {total['error']}"]
    for m in MODES:
        c = counts_of(rows, m)
        lines.append(f"  mode {m:<10} sampled {sum(c.values()):>4}  matched {c['matched']:>4}  no_match {c['no_match']:>4}  "
                     f"rate {_pct(rate(c))}" + ("  (matched by title and artist only: weaker, judged on its own)"
                                                if m == MODE_TITLE_ONLY else ""))
    lines += [f"match rate overall (matched / (matched + no_match)): {_pct(rate(total))}  [{comparable} compared]",
              f"search errors: {total['error']} of {searched} searched ({_pct(total['error'] / searched if searched else None)})",
              "gate (#92): matched >= 80% AND wrong <= 2%; the title-only mode is judged on its own wrong rate"]
    for name, mode in (("overall", None), *[(m, m) for m in MODES]):
        s = wrong_stats(rows, mode)
        if not s["matched"]:
            continue
        w = wrong_rate(s)
        checked = s["ok"] + s["wrong"]
        lines.append(f"wrong rate {name}: " + (f"{s['wrong']} of {checked} labelled ({_pct(w)}); limit {mr.MAX_WRONG_RATE:.0%}: "
                                               f"{'within' if w <= mr.MAX_WRONG_RATE else 'EXCEEDED'}" if checked else "not labelled")
                     + f"  [{s['matched']} matched rows, {s['unlabelled']} unlabelled"
                     + (f", {s['other']} with an unrecognised label (use ok or wrong)" if s["other"] else "") + "]"
                     + ("  only a hint: fewer than 30 labelled" if 0 < checked < mr.MIN_SAMPLE else ""))
    lines.append(f"verdict: {verdict(rows)}")
    if comparable < mr.MIN_SAMPLE:
        lines.append(f"note: fewer than {mr.MIN_SAMPLE} compared tracks; the numbers are only a hint")
    if searched and total["error"] / searched > mr.ERROR_SHARE_WARN:
        lines.append(f"WARNING: verdict may be misleading: {total['error'] / searched:.0%} of searches failed")
    pats = mr.pattern_counts(rows)
    if pats:
        lines.append("top failing patterns (no_match rows, one primary pattern each):")
        lines += [f"  {n:>4}  {name}" for name, n in pats]
    low = lowest_title_only(rows)
    if low:
        lines.append("lowest-similarity title-only matches (eyeball: is it the same recording?):")
        lines += [f"  {_num(r.similarity, 3)}  {clean_text(r.artist)} - {clean_text(r.title)}  ->  "
                  f"{clean_text(r.matched_title)} / {clean_text(r.matched_artists)} ({_num(r.matched_duration)} s)" for r in low]
    bad = mr.worst_rows(rows)
    if bad:
        lines.append("lowest-similarity failures (a wrong reason here means the matcher needs work):")
        lines += [f"  {_num(r.similarity, 3) or '  -  '}  {r.mode:<10} {r.reason:<24} {clean_text(r.artist)} - "
                  f"{clean_text(r.title)}  closest: {clean_text(r.matched_title) or '-'}" for r in bad]
    return lines


def summary_lines(rows: list[Row], outcomes: list[ArtistOutcome], stop: str | None, csv_path: Path | None,
                  info: RunInfo) -> list[str]:
    listed, unique = sum(o.listed for o in outcomes), sum(o.unique for o in outcomes)
    nodur = sum(o.unique_no_duration for o in outcomes)
    s_nodur = sum(1 for r in rows if r.duration is None)
    lines = ["", "== Last.fm match-rate report ==",
             f"artists ranked: {info.ranked}  sampled (evenly spaced over the ranking): {len(outcomes)}",
             f"tracks listed: {listed}  unique after collapsing duplicate recordings: {unique}",
             f"Last.fm duration: {unique - nodur} with, {nodur} without ({_pct(nodur / unique if unique else None)} "
             f"without); in the sample: {len(rows) - s_nodur} with, {s_nodur} without",
             f"Last.fm calls: {info.last_calls} (budget {info.last_budget})  YouTube searches: {info.searches} "
             f"(budget {info.search_budget})",
             info.stored_note]
    lines += result_lines(rows)
    if stop:
        lines.append(f"STOPPED EARLY: {stop}; the numbers cover only what ran")
    lines.append("per artist:")
    for oc in outcomes:
        label = f"rank {oc.rank} {clean_text(oc.name)}"
        if oc.error:
            lines.append(f"  {label}: FAILED {clean_text(oc.error)}")
        else:
            c = oc.counts
            lines.append(f"  {label}: {oc.sampled} sampled of {oc.unique} unique ({oc.listed} listed, "
                         f"{oc.unique_no_duration} without duration)  matched {c['matched']}  no_match {c['no_match']}  "
                         f"stored {c['stored']}  error {c['error']}  rate {_pct(rate(c))}")
    if csv_path:
        lines.append(f"CSV: {csv_path}")
        lines.append("next: label matched rows ok/wrong in the `reviewed` column, then: "
                     f"python ingest/lastfm_report.py --summarize {csv_path.name}")
    return lines


# ---- CLI ----

def _int_range(lo: int, hi: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return n
    return parse


def default_out() -> str:
    return mr.default_out().replace("match-report-", "match-report-lastfm-", 1)


def stored_check(secrets: list[str], reader: Callable[[], list[tuple]] = read_stored_rows) -> tuple[list[tuple] | None, str]:
    """(rows, note). One read-only parameterless SELECT through the ingest D1 helper. Without the D1 variables or on
    any D1 failure the check is skipped with a clear line; it never fails the report."""
    need = ("CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID", "INGEST_CLOUDFLARE_API_TOKEN")
    if not all(os.environ.get(k) for k in need):
        return None, f"stored check: skipped (needs {', '.join(need)} in .env)"
    try:
        rows = reader()
    except Exception as e:  # skipped with a reason, never a crash; the message is redacted
        return None, f"stored check: skipped (D1 read failed: {type(e).__name__}: {clean_text(redact(str(e), secrets))[:120]})"
    return rows, f"stored check: read {len(rows)} stored rows from D1 (read-only SELECT)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Last.fm match-rate report (no download, no upload).")
    ap.add_argument("--lastfm-user", help="Last.fm user name (public profile); not printed")
    ap.add_argument("--years", type=_int_range(1, 10), default=DEFAULT_YEARS)
    ap.add_argument("--artists", type=_int_range(1, 1000), default=DEFAULT_ARTISTS, help="size of the ranking")
    ap.add_argument("--sample-artists", type=_int_range(1, MAX_SAMPLE_ARTISTS), default=DEFAULT_SAMPLE_ARTISTS,
                    help=f"artists out of the ranking whose tracks are enumerated (default {DEFAULT_SAMPLE_ARTISTS})")
    ap.add_argument("--max-tracks", type=mr._bounded_int, default=mr.DEFAULT_MAX_TRACKS,
                    help=f"hard cap on tracks searched (default {mr.DEFAULT_MAX_TRACKS})")
    ap.add_argument("--max-minutes", type=_int_range(1, MAX_MINUTES_LIMIT), default=DEFAULT_MAX_MINUTES,
                    help=f"hard cap on wall time (default {DEFAULT_MAX_MINUTES}); checked between tracks and artists")
    ap.add_argument("--min-interval", type=mr._interval, default=mr.MIN_INTERVAL_FLOOR,
                    help="seconds between two YouTube Music searches (default and minimum 1)")
    ap.add_argument("--check-stored", action="store_true",
                    help="also read the stored tracks from D1 (one read-only SELECT) and skip tracks already stored")
    ap.add_argument("--out", default=None, help="CSV path, must be named match-report*.csv (default: in the cwd)")
    ap.add_argument("--summarize", metavar="CSV", help="offline: print the summary of a (labelled) CSV and exit")
    args = ap.parse_args(argv)
    if args.summarize:
        try:
            rows = read_rows(args.summarize)
        except ReportError as e:
            print(f"ERROR {e}", file=sys.stderr)
            return 1
        print("\n".join(["", "== Last.fm match-rate report (from CSV) =="] + result_lines(rows)))
        return 0
    secrets: list[str] = []
    rows: list[Row] = []
    outcomes: list[ArtistOutcome] = []
    info, stop, out_path = RunInfo(), None, None
    try:
        if not args.lastfm_user:
            raise ReportError("--lastfm-user is required (or --summarize CSV)")
        lastfm.parse_user(args.lastfm_user)
        if args.sample_artists > args.max_tracks:
            raise ReportError("--max-tracks must be at least --sample-artists")
        out_path = mr.validate_out(args.out or default_out())
        tg_check.load_env()
        key = lastfm.api_key_from_env()
        secrets = [key]
        start = time.monotonic()
        info.last_budget = ranking_call_bound(args.years) + args.sample_artists * CALL_BUDGET_PER_ARTIST
        info.search_budget = mr.SEARCHES_PER_TRACK_BOUND * args.max_tracks
        client = lastfm.LastfmClient(key, max_calls=info.last_budget, out=lambda s: print(s, file=sys.stderr))
        ranking = lastfm.top_artists(client, args.lastfm_user, args.years, max_artists=args.artists,
                                     out=lambda s: print(s, file=sys.stderr))
        info.ranked = len(ranking)
        stored_rows = None
        info.stored_note = "stored check: not requested (--check-stored)"
        if args.check_stored:
            stored_rows, info.stored_note = stored_check(secrets)
        from ytmusicapi import YTMusic  # imported late: the pure parts and the tests need no network library
        ps = mr.PacedSearch(YTMusic(), Throttle(), args.min_interval, info.search_budget)

        def enumerate_songs(a: dict) -> list[dict]:
            return lastfm_tracks.artist_tracks(client, a["name"], a.get("mbid"), ALBUMS_PER_ARTIST, CALL_BUDGET_PER_ARTIST,
                                               out=lambda s: None).records
        try:
            outcomes, stop = run(pick_artists(ranking, args.sample_artists), enumerate_songs, ps, args.max_tracks, rows,
                                 secrets, stored_rows, deadline=start + args.max_minutes * 60)
        finally:
            info.last_calls, info.searches = client.calls, ps.searches
            mr.write_csv(out_path, rows, COLUMNS)  # also after an interrupt: what was learned is kept
    except (ReportError, lastfm.LastfmError, tg_check.EnvSyntaxError, OSError) as e:
        print(f"ERROR {redact(str(e), secrets)}", file=sys.stderr)
        return 1
    for line in summary_lines(rows, outcomes, stop, out_path, info):
        print(line)
    return 2 if stop is not None or any(o.error for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())

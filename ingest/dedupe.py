"""Collapse duplicate recordings of one song across releases (single + album + deluxe + compilation) before any
download. Pure: no I/O, no network, the input records are never modified (the same dict objects are returned).

Input: the song records of ingest/artist_tracks.py (song_id, name, artists, album_artist, year, duration in seconds,
and the extra album_type / release_date / explicit fields). Spotify ISRC is not available (one call per track), so
the identity of a recording is a heuristic key:

  key = normalised title + primary artist + duration within +-DURATION_TOLERANCE_S (+ variant, see below)

* Title: lower-cased, NFKC, punctuation-insensitive (match.normalize). Remaster / deluxe / expanded / anniversary /
  bonus-track / explicit / clean tags and live markers are removed from the KEY only; the record keeps its title.
  A tag is removed only when the whole bracket group or the whole " - suffix" is made of tag words, so "(feat. X)",
  "(Remix)", "(Acoustic)", "(Instrumental)", "- Live Wire" stay part of the key and never merge with the studio cut.
* Primary artist: the first track artist (the album artist of a compilation is "Various Artists"); the album artist
  only when the track lists none.
* Variants: live and explicit recordings are separate recordings by default (merge_variants=False). With
  merge_variants=True the live and explicit/clean distinction is dropped from the key.
* Duration clustering is NOT chained. Records of one (title, artist, variant) group are visited best-first (see
  below); a record is dropped only when its duration is within the tolerance of a record that is already KEPT,
  otherwise it becomes a kept record itself. So every dropped record is within +-3 s of the kept one it is
  collapsed into, never of another dropped one: 200, 202.5, 205 gives two recordings (200 and 205), not one.
  A consequence is idempotence: kept records are pairwise further apart than the tolerance, so a second run
  changes nothing.
* Which one is kept: the earliest release date (a reduced-precision date, year or year-month, sorts after the
  full dates of that period); on a tie album over single over compilation over unknown; then input order.
* Records that cannot be compared are always kept and counted as `uncomparable`: no usable duration (missing,
  zero, negative, NaN, bool, not a number), no title, or no artist, or a title / artist name longer than
  MAX_TEXT_LEN characters (no regex runs on it, so one corrupt record cannot stall a run).
* Output order is the input order of the kept records. Complexity O(n log n).
"""
import bisect
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable

from match import DURATION_TOLERANCE_S, normalize

MAX_TEXT_LEN = 300# titles / artist names longer than this are uncomparable: bounds the regex work per record
_EPS = 1e-9 # float noise on the +-3.0 s boundary (durations are ms / 1000)
_TYPE_RANK = {"album": 0, "single": 1, "compilation": 2}

_GROUP = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")
_DASH = re.compile(r"\s+[-–—]\s+")
_WORD = re.compile(r"[^\W_]+")
_TAG_KEYWORD = {"remaster", "remastered", "remasters", "deluxe", "expanded", "anniversary", "bonus", "explicit", "clean"}
_TAG_FILLER = {"digitally", "edition", "version", "track", "special", "super", "the", "and", "collectors", "collector",
               "s", "tracks", "re", "reissue"}
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)?")
_LIVE = re.compile(r"(?:\d{4}\s+)?live(?:\s+(?:at|in|from|on|version|recording|session|performance|\d{4})\b.*)?")
_NOT_SAME = {"remix", "mix", "rmx", "edit", "acoustic", "instrumental", "demo", "cover", "karaoke", "rework",
             "reprise", "vip", "dub", "flip", "bootleg", "slowed", "sped", "nightcore", "remake"}


@dataclass
class DedupeResult:
    kept: list[dict]
    listed: int
    unique: int
    dropped: int
    uncomparable: int  # kept without comparison (no usable duration, title or artist); included in `unique`
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (dropped song_id, kept song_id), only if asked for


def _classify(text: str) -> str | None:
    """'live', 'explicit', 'clean', 'tag' for a removable tag text, None when it is part of the title."""
    words = _WORD.findall(text)
    if not words or _NOT_SAME.intersection(words):
        return None
    if _LIVE.fullmatch(text.strip()):
        return "live"
    if all(w in _TAG_KEYWORD or w in _TAG_FILLER or _ORDINAL.fullmatch(w) for w in words) and _TAG_KEYWORD.intersection(words):
        if "explicit" in words:
            return "explicit"
        if "clean" in words:
            return "clean"
        return "tag"
    return None


def title_key(title: str) -> tuple[str, bool, bool]:
    """(key text, is_live, is_explicit) for a displayed title. The key text has the removable tags dropped."""
    if len(title) > MAX_TEXT_LEN:  # no regex runs on it; an empty key makes the record uncomparable (kept)
        return "", False, False
    text = unicodedata.normalize("NFKC", title).lower()
    live = explicit = False

    def bracket(m: re.Match) -> str:
        nonlocal live, explicit
        kind = _classify(m[1])
        if kind is None:
            return m[0]
        live, explicit = live or kind == "live", explicit or kind == "explicit"
        return ""

    text = _GROUP.sub(bracket, text)
    for m in _DASH.finditer(text):  # first separator whose whole remainder is a tag
        kind = _classify(text[m.end():])
        if kind is not None:
            live, explicit = live or kind == "live", explicit or kind == "explicit"
            text = text[:m.start()]
            break
    return normalize(text) or text.strip(), live, explicit


def _duration(rec: dict) -> float | None:
    d = rec.get("duration")
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d <= 0:
        return None
    return float(d)


def _primary_artist(rec: dict) -> str:
    artists = rec.get("artists")
    first = artists[0] if isinstance(artists, list) and artists and isinstance(artists[0], str) else None
    alb = rec.get("album_artist")
    name = first or (alb if isinstance(alb, str) else "") or ""
    return normalize(name) if len(name) <= MAX_TEXT_LEN else ""  # over-long: uncomparable


def _date_key(rec: dict) -> tuple[int, int, int]:
    rd, year = rec.get("release_date"), rec.get("year")
    m = re.fullmatch(r"([0-9]{4})(?:-([0-9]{2}))?(?:-([0-9]{2}))?", rd) if isinstance(rd, str) else None
    if m:
        y = int(m[1])
        mo = int(m[2]) if m[2] and 1 <= int(m[2]) <= 12 else 13
        d = int(m[3]) if m[3] and mo != 13 and 1 <= int(m[3]) <= 31 else 32
        return y, mo, d
    if isinstance(year, int) and not isinstance(year, bool) and 0 < year < 10000:
        return year, 13, 32
    return 9999, 13, 32


def _rank(rec: dict, index: int) -> tuple:
    t = rec.get("album_type")
    return (_date_key(rec), _TYPE_RANK.get(t, 3) if isinstance(t, str) else 3, index)


def collapse(records: Iterable[dict], merge_variants: bool = False, tolerance: float = DURATION_TOLERANCE_S,
             collect_pairs: bool = False) -> DedupeResult:
    recs = list(records)
    groups: dict[tuple, list[tuple[tuple, float, int]]] = {}
    keep = [False] * len(recs)
    uncomparable = 0
    for i, rec in enumerate(recs):
        if not isinstance(rec, dict):
            raise TypeError(f"record {i} is not a dict")
        name, dur, artist = rec.get("name"), _duration(rec), _primary_artist(rec)
        title, live, explicit = title_key(name) if isinstance(name, str) else ("", False, False)
        explicit = explicit or rec.get("explicit") is True
        if dur is None or not title or not artist:
            keep[i] = True
            uncomparable += 1
            continue
        key = (title, artist) if merge_variants else (title, artist, live, explicit)
        groups.setdefault(key, []).append((_rank(rec, i), dur, i))
    pairs: list[tuple[str, str]] = []
    for members in groups.values():
        members.sort()  # best (earliest) first
        durs: list[float] = []  # durations of kept records, sorted; pairwise further apart than the tolerance
        owner: dict[float, tuple[tuple, int]] = {}
        for rank, dur, i in members:
            lo = bisect.bisect_left(durs, dur - tolerance - _EPS)
            hi = bisect.bisect_right(durs, dur + tolerance + _EPS)
            near = [owner[d] for d in durs[lo:hi]]  # at most two kept records fit in a window of 2 * tolerance
            if near:
                if collect_pairs:
                    pairs.append((str(recs[i].get("song_id")), str(recs[min(near)[1]].get("song_id"))))
                continue
            keep[i] = True
            bisect.insort(durs, dur)
            owner[dur] = (rank, i)
    kept = [r for r, k in zip(recs, keep) if k]
    return DedupeResult(kept=kept, listed=len(recs), unique=len(kept), dropped=len(recs) - len(kept),
                        uncomparable=uncomparable, pairs=pairs)


@dataclass
class StoredResult:
    new: list[dict]  # the records that are not stored yet, input order, the same dict objects
    listed: int
    stored: int  # records found among the stored rows (dropped)
    stored_without_duration: int  # of `stored`: matched on artist + title key only (a side had no usable duration)


def drop_stored(records: Iterable[dict], stored_rows: Iterable[tuple], merge_variants: bool = False,
                tolerance: float = DURATION_TOLERANCE_S) -> StoredResult:
    """Cross-source dedupe: the records that are NOT already stored. `stored_rows` are (title, primary artist,
    duration_s) read from D1. Pure, no I/O, the records are never modified.

    A record is stored when a row has the same normalised artist, the same title key (title_key: live and explicit
    variants stay distinct unless merge_variants) and a duration within +-tolerance. When a side has no usable
    duration only artist + title key are compared (counted in stored_without_duration). Records that cannot be
    compared (no title, no artist, over-long text) are never dropped. Stored rows that cannot be compared are
    ignored. Complexity O((n + m) log m)."""
    index: dict[tuple, list] = {}  # key -> [sorted durations, has a row without duration]
    for row in stored_rows:
        title, artist, dur = row
        if not isinstance(title, str) or not isinstance(artist, str):
            continue
        key_title, live, explicit = title_key(title)
        name = normalize(artist) if len(artist) <= MAX_TEXT_LEN else ""
        if not key_title or not name:
            continue
        entry = index.setdefault((key_title, name) if merge_variants else (key_title, name, live, explicit), [[], False])
        d = _duration({"duration": dur})
        if d is None:
            entry[1] = True
        else:
            entry[0].append(d)
    for entry in index.values():
        entry[0].sort()
    recs = list(records)
    new: list[dict] = []
    without_duration = 0
    for i, rec in enumerate(recs):
        if not isinstance(rec, dict):
            raise TypeError(f"record {i} is not a dict")
        name, dur, artist = rec.get("name"), _duration(rec), _primary_artist(rec)
        title, live, explicit = title_key(name) if isinstance(name, str) else ("", False, False)
        explicit = explicit or rec.get("explicit") is True
        entry = index.get((title, artist) if merge_variants else (title, artist, live, explicit)) if title and artist else None
        if entry is None:
            new.append(rec)
            continue
        durs, row_without = entry
        if dur is None:  # compared on artist + title key only
            without_duration += 1
            continue
        lo = bisect.bisect_left(durs, dur - tolerance - _EPS)
        if lo < len(durs) and durs[lo] <= dur + tolerance + _EPS:
            continue
        if row_without:  # a stored row without a duration: artist + title key only
            without_duration += 1
            continue
        new.append(rec)
    return StoredResult(new=new, listed=len(recs), stored=len(recs) - len(new), stored_without_duration=without_duration)


def _safe(value: str) -> str:
    """An id for a log line: control characters (newlines included) replaced, length bounded."""
    return "".join(c if c.isprintable() else "?" for c in value[:64])


def summary(result: DedupeResult) -> str:
    return f"listed {result.listed}, unique {result.unique}, dropped {result.dropped}"


def report(result: DedupeResult, out: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
           show_pairs: bool = False) -> None:
    """Prints the counts only. Pairs (song ids, never titles) are printed only when show_pairs is set."""
    out(summary(result))
    if show_pairs:
        for dropped, kept in result.pairs:
            out(f"dropped {_safe(dropped)} -> kept {_safe(kept)}")

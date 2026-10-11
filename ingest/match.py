"""Match a Spotify track to a YouTube Music video.

spotdl's built-in filter compares artist names literally, which rejects every result when Spotify
spells an artist in Latin and YouTube Music in Cyrillic. We search with a ladder of queries and
accept a result only if the title is similar AND the duration is close, so a wrong video is never
accepted just because the artist name looks different.
"""
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

DURATION_TOLERANCE_S = 3
TITLE_MIN_SIMILARITY = 0.85

_DIGRAPHS = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"),
             ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("ye", "е"), ("ju", "ю"), ("ja", "я")]
_LETTERS = dict(zip("abvgdezijklmnoprstufhcyxqw", "абвгдезийклмнопрстуфхцыксв"))


def transliterate_to_cyrillic(text: str) -> str:
    """Approximate Latin -> Russian Cyrillic. Lossy by nature; used only for search queries and fuzzy compare."""
    out = text.lower()
    for lat, cyr in _DIGRAPHS:
        out = out.replace(lat, cyr)
    return "".join(_LETTERS.get(c, c) for c in out)


def has_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return re.sub(r"[^\w]+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


# Title-only mode (#151): for a track WITHOUT a usable duration (Last.fm gives almost none). The +-3 s check is what
# protects the duration path from a wrong video, so this mode compensates: a stricter title threshold (0.93 against
# 0.85: tolerates "Song - Remastered 2011" style noise, not a different title), a MANDATORY artist check (the
# Latin/Cyrillic transliteration compare, so a Cyrillic spelling does not veto), a sanity range for the candidate's
# duration (600 s is also about the 20 MB Telegram limit), and no variant marker the requested title lacks.
# Trust trade-off accepted by the owner in #147: more wrong-video risk than the duration path.
TITLE_ONLY_TITLE_MIN_SIMILARITY = 0.93
# 0.6 was compared on the full names, so "the kooks" / "the motion" scored 0.63 (#241): the shared article and a similar
# length carry the ratio, not the name. The article is now dropped from both sides before the compare, and the bar is 0.7.
TITLE_ONLY_ARTIST_MIN_SIMILARITY = 0.7
TITLE_ONLY_MIN_DURATION_S = 60
TITLE_ONLY_MAX_DURATION_S = 600
MODE_TITLE_ONLY = "title-only"
MODE_DURATION = "duration"
# Same words as dedupe._NOT_SAME plus "live" (dedupe imports this module, so it cannot be imported from there; a test
# keeps the two in step).
_VARIANT_WORDS = {"live", "remix", "mix", "rmx", "edit", "acoustic", "instrumental", "demo", "cover", "karaoke",
                  "rework", "reprise", "vip", "dub", "flip", "bootleg", "slowed", "sped", "nightcore", "remake"}


# Relaxed title comparison (#194): a SECOND pass that find_match runs only when the strict pass (the code above, byte for
# byte what it was) found nothing. It drops version / format noise that does not change the recording and compares the
# rest, but it never makes different recordings equal: the variant markers (live + its venue, acoustic, remix, ...) of
# the two titles must be the SAME (a repeat counts), however they are spelled, bracketed or ordered. The thresholds are the ones of
# the calling mode, unchanged.
_RELAXED_MARKERS = _VARIANT_WORDS | {"extended", "radio", "8d", "reverb", "reverbed"}
_GROUP = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")
_DASH_TAIL = re.compile(r"\s+[-\u2013\u2014]\s+([^-\u2013\u2014]*)$")
_NOISE = re.compile(
    r"(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?"  # (Remastered 2011), - 2011 Remaster
    r"|(?:19|20)\d{2}"                                                                    # (1971)
    r"|(?:album|single)\s+version"
    r"|explicit(?:\s+version)?")
_LIVE_PREPOSITIONS = {"at", "in", "from", "on"}
MAX_RELAXED_TITLE_LEN = 300  # real titles are about 100 characters; bounds the regex work per title


def _noise_stripped(text: str) -> str:
    """Remove a bracket group or a ' - suffix' whose WHOLE content is noise. Never empties the title."""
    stripped = _GROUP.sub(lambda m: "" if _NOISE.fullmatch(m[1].strip()) else m[0], text)
    m = _DASH_TAIL.search(stripped)
    if m and _NOISE.fullmatch(m[1].strip()):
        stripped = stripped[:m.start()]
    return stripped if re.search(r"\w", stripped) else text


def _latin_without_marks(text: str) -> str:
    """Drop combining marks from Latin letters only (so a Cyrillic 'й' keeps its breve)."""
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)
        out.append("".join(c for c in base if not unicodedata.combining(c)) if base[0].isascii() else ch)
    return "".join(out)


def artist_similarity(a: str, b: str) -> float:
    """Artist compare for title-only mode: a leading article ('the', 'a', 'an') is not part of the name."""
    def bare(x: str) -> str:
        n = normalize(x)
        m = re.match(r"(?:the|a|an) (.+)", n)
        return m[1] if m else n
    return SequenceMatcher(None, bare(a), bare(b)).ratio()


def title_parts(title: str) -> tuple[list[str], tuple[str, ...]]:
    """(core words, markers). Markers are the variant words found in the title; for 'live' also its venue words, so
    'Live at X' and 'Live at Y' differ and 'live in X (acoustic)' equals 'Acoustic / Live At X, 2005'."""
    if len(title) > MAX_RELAXED_TITLE_LEN:  # no regex runs on it: the strict pass alone decides
        return [], ()
    text = unicodedata.normalize("NFKC", title).lower().replace("ё", "е")
    text = _latin_without_marks(_noise_stripped(text))
    text = text.replace("&", " and ")
    words = normalize(re.sub(r"(?<=\w)['\u2019](?=\w)", "", text)).split()  # don't -> dont
    if len(words) < 2:  # a one-word title keeps its apostrophe: Hell / He'll, Well / We'll stay apart
        words = normalize(text).split()
    core: list[str] = []
    markers: list[str] = []
    live_seen = False
    venue: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in _RELAXED_MARKERS:
            markers.append(w)
            if i + 1 < len(words) and words[i + 1] == "version":  # 'acoustic version' == 'acoustic'
                i += 1
            if w == "live":
                live_seen = True
                if i + 1 < len(words) and words[i + 1] in _LIVE_PREPOSITIONS:
                    i += 1
                    j = i + 1
                    while j < len(words) and words[j] not in _RELAXED_MARKERS:
                        venue.append(words[j])  # numbers included: another year is another recording
                        j += 1
                    i = j - 1
        elif live_seen and re.fullmatch(r"\d{4}", w):
            venue.append(w)
        else:
            core.append(w)
        i += 1
    if venue:
        markers.append("live@" + " ".join(sorted(venue)))
    return core, tuple(sorted(markers))


def _singular_last(words: list[str]) -> list[str]:
    """Bounded plural tolerance: only the LAST word, only a single trailing 's' of a word of 4+ letters."""
    if len(words) >= 2 and len(words[-1]) >= 4 and words[-1].endswith("s") and not words[-1].endswith(("ss", "us", "is")):
        return words[:-1] + [words[-1][:-1]]
    return words


def relaxed_similarity(a: str, b: str) -> float:
    """0.0 when the variant markers differ; otherwise the similarity of the noise-free titles. There is no prefix
    rule: 'Bye Bye' / 'Bye Bye Bye' are different songs and the duration path has no artist veto."""
    (ca, ma), (cb, mb) = title_parts(a), title_parts(b)
    if ma != mb or not ca or not cb:
        return 0.0
    ca, cb = _singular_last(ca), _singular_last(cb)
    sim = SequenceMatcher(None, " ".join(ca), " ".join(cb)).ratio()
    return sim


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: str
    duration_s: int
    score: float
    mode: str = MODE_DURATION  # MODE_TITLE_ONLY when the match was made without a duration (#151)
    relaxed: bool = False  # True when only the relaxed title pass (#194) accepted it: traceable in the logs
    # Cover fields (#248), read from the chosen search result; used only for the album cover guard below and by #251.
    album: str | None = None          # the result's album name
    artist_id: str | None = None      # browse id of the result's first artist
    artist_name: str | None = None    # name of the result's first artist
    thumbnail_base: str | None = None  # first thumbnail URL with its "=..." size suffix cut off


COVER_SIZE_SUFFIX = "=w1200-h1200-l90-rj"  # verified 2026-10-11: HTTP 200, JPEG, 1200x1200, about 0.33 MB (#245, #248)
# A cover from a match is stored per album for good (COALESCE keeps the first one), so the guard is strict: a missing
# or different album, another edition, another volume or another artist means NO cover.
COVER_ALBUM_MIN_SIMILARITY = 0.9
COVER_ARTIST_MIN_SIMILARITY = 0.8
_EDITION_WORDS = _VARIANT_WORDS | {"deluxe", "edition", "remastered", "remaster", "anniversary", "expanded", "special",
                                   "bonus", "version", "ep", "single", "volume", "vol", "part", "pt", "disc", "cd"}
_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _result_extras(result: dict) -> dict:
    """Cover fields of one search result; every field None when it is missing or not of the expected type."""
    def text(v):
        return v if isinstance(v, str) and v else None
    album = result.get("album")
    artists = [a for a in result.get("artists") or [] if isinstance(a, dict)]
    first = artists[0] if artists else {}
    thumbs = result.get("thumbnails")
    url = thumbs[0].get("url") if isinstance(thumbs, list) and thumbs and isinstance(thumbs[0], dict) else None
    base = url.rsplit("=", 1)[0] if isinstance(url, str) and url.startswith("https://") and "=" in url else None
    return {"album": text(album.get("name")) if isinstance(album, dict) else None,
            "artist_id": text(first.get("id")), "artist_name": text(first.get("name")), "thumbnail_base": text(base)}


def _plural_pair(ww: set, wf: set) -> bool:
    """True when the two word sets differ by a word and the same word plus 's' (Lover / Lovers, Slayer / Slayers):
    near-identical by ratio, but a different album or artist."""
    diff = ww ^ wf
    return any(w + "s" in diff for w in diff)


def artists_match(wanted: str, names: list[str]) -> bool:
    """Cover-only artist check: article-insensitive, Latin vs Cyrillic aware, a bare plural 's' is a different artist."""
    return any(artist_similarity(a, n) >= COVER_ARTIST_MIN_SIMILARITY
               and not _plural_pair(set(normalize(a).split()), set(normalize(n).split()))
               for a in (wanted, transliterate_to_cyrillic(wanted)) for n in names)


def albums_match(wanted: str, found: str) -> bool:
    nw, nf = normalize(wanted), normalize(found)
    if not nw or not nf or SequenceMatcher(None, nw, nf).ratio() < COVER_ALBUM_MIN_SIMILARITY:
        return False
    ww, wf = set(nw.split()), set(nf.split())
    if _plural_pair(ww, wf):
        return False
    if {w for w in ww if w.isdigit() or w in _ROMAN} != {w for w in wf if w.isdigit() or w in _ROMAN}:
        return False  # Vol. 1 / Vol. 2, II / III
    return not (ww ^ wf) & _EDITION_WORDS  # (Deluxe), (Live), Remastered, ...: another release


def cover_url_from_match(album: str | None, artist: str, m) -> tuple[str | None, str]:
    """(cover URL or None, reason). Reason is a fixed class word (never a title or a name): ok, no_album,
    no_thumbnail, album_mismatch, artist_mismatch. `album` is the TRACK's album name (None for an unknown album)."""
    base, found = getattr(m, "thumbnail_base", None), getattr(m, "album", None)
    if not isinstance(base, str) or not base.startswith("https://"):
        return None, "no_thumbnail"
    if not isinstance(album, str) or not album.strip():
        return None, "no_album"
    if not isinstance(found, str):
        return None, "no_match_album"
    if not albums_match(album, found):
        return None, "album_mismatch"
    joined = getattr(m, "artists", None)
    names = [n for n in (getattr(m, "artist_name", None), joined, *(joined.split(", ") if isinstance(joined, str) else []))
             if isinstance(n, str) and n]
    if not names or not artists_match(artist, names):
        return None, "artist_mismatch"
    return base + COVER_SIZE_SUFFIX, "ok"


def queries(title: str, artist: str, album: str | None) -> list[str]:
    qs = [f"{title} {album}" if album else title, f"{artist} {title}"]
    if has_latin(artist):
        qs.append(f"{transliterate_to_cyrillic(artist)} {title}")
    return list(dict.fromkeys(qs))


def score(title: str, artist: str, duration_s: int, result: dict, relaxed: bool = False) -> Candidate | None:
    dur = result.get("duration_seconds")
    if not dur or abs(dur - duration_s) > DURATION_TOLERANCE_S:
        return None
    t_sim = (relaxed_similarity(title, result.get("title", "")) if relaxed
             else similarity(title, result.get("title", "")))
    if t_sim < TITLE_MIN_SIMILARITY:
        return None
    yt_artists = ", ".join(a["name"] for a in result.get("artists") or [])
    a_sim = max(similarity(artist, yt_artists), similarity(transliterate_to_cyrillic(artist), yt_artists))
    # Artist only breaks ties; it must not veto (that is the bug we are working around).
    return Candidate(result["videoId"], result["title"], yt_artists, dur,
                     t_sim + 0.3 * a_sim - 0.02 * abs(dur - duration_s), relaxed=relaxed,
                     **_result_extras(result))


def score_title_only(title: str, artist: str, result: dict, relaxed: bool = False) -> Candidate | None:
    """Scoring without a requested duration (see the constants above). None unless every check passes."""
    dur = result.get("duration_seconds")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) \
            or not TITLE_ONLY_MIN_DURATION_S <= dur <= TITLE_ONLY_MAX_DURATION_S:
        return None
    result_title = result.get("title")
    if not isinstance(result_title, str):
        return None
    t_sim = relaxed_similarity(title, result_title) if relaxed else similarity(title, result_title)
    if t_sim < TITLE_ONLY_TITLE_MIN_SIMILARITY:
        return None
    if not relaxed and set(normalize(result_title).split()) & (_VARIANT_WORDS - set(normalize(title).split())):
        return None  # live, remix, cover, ...: a different recording than the requested one
    names = [a["name"] for a in result.get("artists") or [] if isinstance(a.get("name"), str)]
    joined = ", ".join(names)
    a_sim = max(artist_similarity(a, y) for a in (artist, transliterate_to_cyrillic(artist)) for y in [joined, *names])
    if a_sim < TITLE_ONLY_ARTIST_MIN_SIMILARITY:
        return None
    return Candidate(result["videoId"], result_title, joined, int(dur), t_sim + a_sim, MODE_TITLE_ONLY, relaxed,
                     **_result_extras(result))


def find_match(ytmusic, title: str, artist: str, album: str | None, duration_s: int | None) -> Candidate | None:
    """With a duration (any non-zero value) the +-3 s path below, unchanged. With None or 0 the title-only mode.
    Two passes over the SAME search results (no extra search call): strict first, exactly as before #194; only when it
    finds nothing, the relaxed title comparison (version noise dropped, variant markers must be equal) is tried."""
    def scorer(relaxed: bool):
        if duration_s:
            return lambda r: score(title, artist, duration_s, r, relaxed)
        return lambda r: score_title_only(title, artist, r, relaxed)

    strict, seen = scorer(False), []
    for q in queries(title, artist, album):
        results = ytmusic.search(q, filter="songs", limit=10)
        seen.append(results)
        found = [c for r in results if r.get("videoId") and (c := strict(r))]
        if found:
            return max(found, key=lambda c: c.score)
    relaxed = scorer(True)
    for results in seen:
        found = [c for r in results if r.get("videoId") and (c := relaxed(r))]
        if found:
            return max(found, key=lambda c: c.score)
    return None

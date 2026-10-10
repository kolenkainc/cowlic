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
TITLE_ONLY_ARTIST_MIN_SIMILARITY = 0.6
TITLE_ONLY_MIN_DURATION_S = 60
TITLE_ONLY_MAX_DURATION_S = 600
MODE_TITLE_ONLY = "title-only"
MODE_DURATION = "duration"
# Same words as dedupe._NOT_SAME plus "live" (dedupe imports this module, so it cannot be imported from there; a test
# keeps the two in step).
_VARIANT_WORDS = {"live", "remix", "mix", "rmx", "edit", "acoustic", "instrumental", "demo", "cover", "karaoke",
                  "rework", "reprise", "vip", "dub", "flip", "bootleg", "slowed", "sped", "nightcore", "remake"}


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: str
    duration_s: int
    score: float
    mode: str = MODE_DURATION  # MODE_TITLE_ONLY when the match was made without a duration (#151)


def queries(title: str, artist: str, album: str | None) -> list[str]:
    qs = [f"{title} {album}" if album else title, f"{artist} {title}"]
    if has_latin(artist):
        qs.append(f"{transliterate_to_cyrillic(artist)} {title}")
    return list(dict.fromkeys(qs))


def score(title: str, artist: str, duration_s: int, result: dict) -> Candidate | None:
    dur = result.get("duration_seconds")
    if not dur or abs(dur - duration_s) > DURATION_TOLERANCE_S:
        return None
    t_sim = similarity(title, result.get("title", ""))
    if t_sim < TITLE_MIN_SIMILARITY:
        return None
    yt_artists = ", ".join(a["name"] for a in result.get("artists") or [])
    a_sim = max(similarity(artist, yt_artists), similarity(transliterate_to_cyrillic(artist), yt_artists))
    # Artist only breaks ties; it must not veto (that is the bug we are working around).
    return Candidate(result["videoId"], result["title"], yt_artists, dur,
                     t_sim + 0.3 * a_sim - 0.02 * abs(dur - duration_s))


def score_title_only(title: str, artist: str, result: dict) -> Candidate | None:
    """Scoring without a requested duration (see the constants above). None unless every check passes."""
    dur = result.get("duration_seconds")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) \
            or not TITLE_ONLY_MIN_DURATION_S <= dur <= TITLE_ONLY_MAX_DURATION_S:
        return None
    result_title = result.get("title")
    if not isinstance(result_title, str):
        return None
    t_sim = similarity(title, result_title)
    if t_sim < TITLE_ONLY_TITLE_MIN_SIMILARITY:
        return None
    if set(normalize(result_title).split()) & (_VARIANT_WORDS - set(normalize(title).split())):
        return None  # live, remix, cover, ...: a different recording than the requested one
    names = [a["name"] for a in result.get("artists") or [] if isinstance(a.get("name"), str)]
    joined = ", ".join(names)
    a_sim = max(similarity(a, y) for a in (artist, transliterate_to_cyrillic(artist)) for y in [joined, *names])
    if a_sim < TITLE_ONLY_ARTIST_MIN_SIMILARITY:
        return None
    return Candidate(result["videoId"], result_title, joined, int(dur), t_sim + a_sim, MODE_TITLE_ONLY)


def find_match(ytmusic, title: str, artist: str, album: str | None, duration_s: int | None) -> Candidate | None:
    """With a duration (any non-zero value) the +-3 s path below, unchanged. With None or 0 the title-only mode."""
    scorer = (lambda r: score(title, artist, duration_s, r)) if duration_s else (lambda r: score_title_only(title, artist, r))
    for q in queries(title, artist, album):
        found = [c for r in ytmusic.search(q, filter="songs", limit=10) if r.get("videoId")
                 and (c := scorer(r))]
        if found:
            return max(found, key=lambda c: c.score)
    return None

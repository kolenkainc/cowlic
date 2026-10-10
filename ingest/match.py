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


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: str
    duration_s: int
    score: float


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


def find_match(ytmusic, title: str, artist: str, album: str | None, duration_s: int) -> Candidate | None:
    for q in queries(title, artist, album):
        found = [c for r in ytmusic.search(q, filter="songs", limit=10) if r.get("videoId")
                 and (c := score(title, artist, duration_s, r))]
        if found:
            return max(found, key=lambda c: c.score)
    return None

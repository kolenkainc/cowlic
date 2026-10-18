"""Read-only MusicBrainz/CAA feasibility probe (#395), never an ingest provider.

Input: private JSON list of <=5 objects: album, artist, title, duration_s.
Output: raw metadata and aggregate refusal classes in a NEW private directory.
No credentials, D1, Telegram, audio downloads or matching overrides are used.
"""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import bootstrap_covers as covers
import ingest
import match

UA = "cowlic-artwork-probe/0.1 ( https://github.com/kolenkainc/cowlic )"
MB = "musicbrainz.org"
CAA = "coverartarchive.org"
MAX_JSON = 2 * 1024 * 1024
MBID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")


class Refusal(Exception):
    pass


class Unavailable(Refusal):
    """Stop the entire probe, not just this album."""


def safe_url(url, hosts):
    try:
        parts = urllib.parse.urlsplit(url)
        good = (parts.scheme == "https" and parts.hostname in hosts and parts.port in (None, 443)
                and parts.username is None and parts.password is None and not parts.fragment)
    except (ValueError, TypeError):
        good = False
    if not good:
        raise Refusal("unsafe_url")


class Source:
    def __init__(self, opener=None, clock=time.monotonic, sleep=time.sleep):
        self.opener = opener or ingest._cover_opener()
        self.clock, self.sleep = clock, sleep
        self.last, self.requests = None, 0
        self.deadline = clock() + 180

    def get(self, url, hosts, image=False):
        safe_url(url, hosts)
        if self.requests >= 25 or self.clock() >= self.deadline:
            raise Unavailable("budget_exhausted")
        if self.last is not None:
            self.sleep(max(0, 1.5 - (self.clock() - self.last)))
        self.last = self.clock()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/jpeg" if image else "application/json"})
        try:
            with self.opener.open(req, timeout=15) as response:
                ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if image:
                    if ctype != "image/jpeg":
                        raise Refusal("not_jpeg")
                    data = ingest._read_cover_body(response, self.clock, min(self.deadline, self.clock() + 30))
                    if not data.startswith(b"\xff\xd8"):
                        raise Refusal("not_jpeg")
                    return data
                if ctype != "application/json":
                    raise Refusal("not_json")
                chunks, total = [], 0
                while True:
                    if self.clock() >= self.deadline:
                        raise Unavailable("budget_exhausted")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_JSON:
                        raise Refusal("json_too_large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise Refusal("invalid_json_shape")
                return value
        except urllib.error.HTTPError as error:
            if error.code in (429, 503):
                raise Unavailable(f"http_{error.code}") from None
            raise Refusal(f"http_{error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Unavailable("network_unavailable") from None
        except (ValueError, ingest.IngestError):
            raise Refusal("invalid_response") from None


def artist_names(release):
    return [credit["artist"]["name"] for credit in release.get("artist-credit", [])
            if isinstance(credit, dict) and isinstance(credit.get("artist"), dict)
            and isinstance(credit["artist"].get("name"), str)]


def release_ok(row, release, tracks=False):
    if not isinstance(release.get("title"), str) or not match.albums_match(row["album"], release["title"]):
        return False
    if not match.artists_match(row["artist"], artist_names(release)):
        return False
    if not tracks:
        return True
    found = []
    for medium in release.get("media", []):
        for track in medium.get("tracks", []):
            recording = track.get("recording", {})
            length = track.get("length", recording.get("length"))
            found.append({"title": track.get("title", recording.get("title")),
                          "duration_seconds": length // 1000 if isinstance(length, int) and not isinstance(length, bool) else None})
    return covers.tracklist_has({"tracks": found}, row["title"], row.get("duration_s"))


def probe_one(row, source, directory):
    # Lucene quoted terms escape metacharacters, and urlencode keeps all private names out of logs.
    def quoted(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    params = urllib.parse.urlencode({"query": f'release:{quoted(row["album"])} AND artist:{quoted(row["artist"])}',
                                    "fmt": "json", "limit": 3})
    search = source.get(f"https://{MB}/ws/2/release/?{params}", {MB})
    ingest.atomic_write_json(directory / "search.json", search)
    candidates = [r for r in search.get("releases", []) if isinstance(r, dict) and release_ok(row, r)]
    for number, release in enumerate(candidates[:2]):
        rid = release.get("id", "")
        if not isinstance(rid, str) or not MBID.fullmatch(rid):
            continue
        page = source.get(f"https://{MB}/ws/2/release/{rid}?inc=recordings+artist-credits&fmt=json", {MB})
        ingest.atomic_write_json(directory / f"release-{number}.json", page)
        if not release_ok(row, page, tracks=True):
            continue
        art = source.get(f"https://{CAA}/release/{rid}", {CAA})
        ingest.atomic_write_json(directory / f"art-{number}.json", art)
        for image in art.get("images", []):
            if not isinstance(image, dict) or image.get("front") is not True:
                continue
            url = image.get("thumbnails", {}).get("small")
            # Archive redirects are intentionally NOT enabled. A redirect is evidence of a missing safe fetch path,
            # not permission to fetch arbitrary hosts. This probe never changes the production image allowlist.
            if not isinstance(url, str):
                continue
            data = source.get(url, {"archive.org"}, image=True)
            (directory / "front.jpg").write_bytes(data)
            return "verified_release_and_jpeg"
        return "verified_release_no_front"
    return "no_guarded_release"


def load_rows(path):
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        raise Refusal("input_requires_one_to_five_albums")
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not 1 <= len(row[k]) <= 400
                                           for k in ("album", "artist", "title")):
            raise Refusal("invalid_input")
        duration = row.get("duration_s")
        if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0):
            raise Refusal("invalid_duration")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="new private evidence directory; refuses existing paths")
    args = parser.parse_args()
    os.umask(0o077)
    counts = collections.Counter()
    try:
        rows = load_rows(args.input)
        args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
        source = Source()
        for index, row in enumerate(rows, 1):
            directory = args.output / str(index)
            directory.mkdir(mode=0o700)
            ingest.atomic_write_json(directory / "stored.json", row)
            try:
                outcome = probe_one(row, source, directory)
            except Unavailable as error:
                counts[str(error)] += 1
                break
            except Refusal as error:
                outcome = str(error)
            except (TypeError, KeyError, AttributeError):
                outcome = "invalid_metadata_shape"
            counts[outcome] += 1
        summary = {"selected": len(rows), "attempted": sum(counts.values()), "requests": source.requests, "outcomes": dict(counts)}
        ingest.atomic_write_json(args.output / "summary.json", summary)
        print(json.dumps(summary))
        return 0
    except (Refusal, OSError, ValueError):
        print("Probe refused invalid input/output; no raw details logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Dry run: print the YouTube Music match for every track of a `spotdl save` file. Usage: match_playlist.py FILE.spotdl"""
import json
import sys

from ytmusicapi import YTMusic

from match import find_match

ytm = YTMusic()
for s in json.load(open(sys.argv[1], encoding="utf-8")):
    m = find_match(ytm, s["name"], s["artists"][0], s.get("album_name"), int(s["duration"]))
    shown = f"{m.video_id} {m.duration_s}s [{m.artists}] {m.title[:40]}" if m else "NO MATCH"
    print(f"{s['duration']:>4}s {s['name'][:38]:<38} -> {shown}")

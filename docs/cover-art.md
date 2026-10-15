# Cover art for albums and artists (design, ticket #245, phase 1)

Status: phase 1 (investigation and design). No code, schema or Worker change comes with this page. Phase 2 starts from the
ticket split in section 8.

Legend: **[V]** verified by the author on 2026-10-11 (the evidence is named); **[R]** reported, read from documentation
or memory, or not verified; **[P]** a proposal of this design, not a fact.

## 1. Decision and goal

- Owner decision (2026-10-11, relayed to the author by the coordinator, recorded on #245): "YouTube Music thumbnails for
  albums: OK. YouTube Music thumbnails for artists: OK." So artist photos ARE wanted, and the source for both is YouTube
  Music. Cover Art Archive (CAA) stays a documented fallback only. The Last.fm exclusion stays (its ToS clause 5.1.8,
  [R] as in the ticket, not re-verified).
- Goal: albums and artists of Last.fm-sourced and new-release tracks show an image in Symfonium. Both sources set
  `cover_url` to None today [V `ingest/lastfm_tracks.py:150`, `ingest/new_releases.py:519`], so `process_track` skips the cover
  upload [V `ingest/ingest.py:803`].

## 2. Findings: what YouTube Music returns

How it was checked: the repo venv Python with ytmusicapi 1.12.3, no login, no cookies, one call at a time, 1.5 s apart;
the throwaway scripts live outside the repo. Total: 8 ytmusicapi calls (4 song searches, 1 album search, `get_album`,
`get_artist`, `get_song`) and 2 image downloads from googleusercontent. No sign-in challenge, no HTTP 429 and no error
was seen on any of them [V].

### 2.1 Search results (the matcher's own call, `search(q, filter="songs")`)

Songs: The Kooks "Seaside" and "See the World", Radiohead "Creep", Daft Punk "Get Lucky" (two results each) [V].

| Fact | Evidence |
|---|---|
| Every result carries `thumbnails` | all 8 results [V] |
| Exactly 2 sizes: 60x60 and 120x120 | all 8 results [V] |
| URL shape `https://yt3.googleusercontent.com/<opaque id>=w60-h60-l90-rj` (the 120 variant ends `=w120-h120-l90-rj`) | [V] |
| Square cover art, not a video frame | the results were `MUSIC_VIDEO_TYPE_ATV` (audio track = album art); the full image of the Kooks album was viewed, see 2.2 [V] |
| The result names its album: `album = {name, id: "MPREb_..."}` | [V] |
| Search thumbnails alone are too small (120 px max) | [V] |
| `videoType` can be other than ATV for videos; for those the thumbnail may be a video frame | not tested, the matcher uses only the songs filter so results are ATV [R] |

Note the matched recording can sit on a different album than the stored one: "See the World" returned both the original
album (`MPREb_WX7lAGDexcF`) and "Inside In, Inside Out (15th Anniversary Deluxe)" (`MPREb_RrFXalyZWdY`); "Creep" by a
cover act appeared as second result [V]. The first result is what the matcher keeps. A cover from a compilation or a
deluxe edition is a mismatch risk (section 7).

### 2.2 Larger sizes

- The size is in the URL suffix and can be edited. Requested `=w1200-h1200-l90-rj` for the Kooks album: HTTP 200,
  `image/jpeg`, 1200x1200, 326 636 bytes [V]. The image is the real cover (white sleeve, "Inside In / Inside Out").
- `get_album(MPREb_WX7lAGDexcF)` lists 4 sizes: 60, 120, 226, 544 square [V]. So the API itself offers 544; the URL edit
  gives more. The largest source size is not known: whether `w1200` is upscaled from a smaller original was not checked [R].
- `get_song(videoId)` for the ATV id returns the same base image with 60/120/226/544 in `videoDetails.thumbnail`; its
  `microformat` thumbnail is 1280x720 (a video frame: do not use) [V].
- Conclusion: no extra request is needed to get a good album cover. Take the first search result's `thumbnails[0].url`,
  cut the `=...` suffix, append `=w1200-h1200-l90-rj` (or `w800`). The suffix grammar is undocumented [R]: treat a failed
  download as "no cover" and never depend on it.

### 2.3 Album page versus search thumbnail

`get_album` is not needed for the image (same base id, 4 listed sizes) [V]. It is useful only to confirm the album
title/year before accepting the cover (the matched album can differ, 2.1) and costs one more call [V]. Searching with
`filter="albums"` is fuzzy: for "The Kooks Inside In Inside Out" the first hit was "Inside In / Inside Out (Acoustic / Live
At Abbey Road, 2005)", not the studio album [V]. So the album id from the matched SONG is a better anchor than an album
search.

### 2.4 What Symfonium and our Worker need

We store a JPEG as a Telegram photo (`sendPhoto`, largest photo kept, image cap 10 MB in `COVER_MAX_BYTES`
[V `ingest/ingest.py:57`, `:892`]). The 1200 px JPEG is 0.33 MB [V]. Telegram `sendPhoto` limits (10 MB, width+height
under 10 000, ratio under 20) are [R]. The Worker returns the file with `content-type: image/jpeg`
[V `worker/src/index.ts` `cover`] and ignores the `size` parameter of `getCoverArt` [V]: clients scale themselves.

### 2.5 Artists (`get_artist`, one call for The Kooks)

- Keys include `thumbnails`, `channelId`, `name`, `subscribers`, `monthlyListeners` [V].
- The thumbnails are a wide BANNER, 5 sizes, all 2.4:1: 540x225, 816x340, 1440x600, 1920x800, 2880x1200, URL suffix
  `=w540-h225-p-l90-rj` [V]. Not square and not an avatar.
- A square crop can be requested: `=w800-h800-p-l90-rj` gave HTTP 200, `image/jpeg`, 800x800, 257 427 bytes, and the
  image is a good group photo of the band, not a distorted banner [V, viewed, one artist only]. Whether the crop is as good
  for every artist (solo artists, off-centre faces) is not verified [R].
- The artist id for `get_artist` comes from the matched song result: `artists[0].id` (`UCVnzbcX5a...`) worked; the returned
  `channelId` is a different value [V]. Not every search result has an artist id (a few have `None`) [R].
- A further call to `get_artist` is needed once per NEW artist (cacheable by `artists.id`); the search result has no artist
  photo [V].

## 3. Options

| Option | Albums | Artists | Cost | Verdict |
|---|---|---|---|---|
| A. YouTube Music thumbnail via the matched song | yes, square, 1200 px, no extra call | needs 1 `get_artist` per new artist | none beyond the match we already make | RECOMMENDED (owner-approved) |
| B. A plus `get_album` to verify the album | same | same | +1 call per album | optional guard for mismatches, section 7 |
| C. MusicBrainz plus Cover Art Archive via mbid | partial coverage | no (albums only) | 2 requests per album, 1 req/s | fallback only, section 5 |
| D. Wikimedia Commons / Wikidata artist photos | no | partial, per-file licences | name to Wikidata resolution, hard to automate safely | not recommended |
| E. Last.fm images | excluded | excluded | | rejected (ToS, ticket) |
| F. Spotify | closed (403) | closed | | rejected (ticket) |

## 4. Licensing and ToS notes

None of this is legal advice. The images are copyrighted by labels and artists. We store and serve them privately to one
owner, like the audio.

- **YouTube Music thumbnails.** YouTube's Terms of Service (https://www.youtube.com/t/terms, read via WebFetch; the page
  came back in Croatian and the author paraphrases it [V as paraphrase]): the user may not access, copy, download or
  distribute Content except as the Service permits or with written permission; may not access the Service by automated
  means (robots, botnets, scrapers) except public search engines under robots.txt; may use the Service for personal,
  non-commercial viewing only. The text has no explicit sentence about thumbnails; "Content" includes graphics and
  photographs [V]. Consequence: automated download of thumbnails is in the same risk class as the audio ingest and the
  ytmusicapi search that the project already does [R, project judgement], and it is inside the owner's approval of
  2026-10-11. Keep it personal, never publish the images, never redistribute (the repo and the Worker are private use;
  the repo is public, so NEVER commit an image). The ToS risk is not "high" beyond what the project already accepts, but
  it is not zero: the owner has accepted it.
- **ytmusicapi** is an unofficial API client; its terms status is [R].
- **Cover Art Archive / MusicBrainz.** The MusicBrainz data licence page: core data CC0, supplementary data CC BY-NC-SA
  3.0; it also says "The MusicBrainz dataset does not contain cover art" [V https://musicbrainz.org/doc/About/Data_License].
  The CAA page names NO licence for the images: images are "collected for archival purposes", "Use the images at your own
  risk", "be respectful of the rights of the artists and labels" [V https://musicbrainz.org/doc/Cover_Art_Archive]. So
  the CAA images are NOT openly licensed; they carry the original copyright. Personal private use is the same risk class as
  YouTube thumbnails [R]. The page links a DMCA policy that was not read [R].
- **Wikimedia Commons (artist photos).** Files are mostly CC BY, CC BY-SA, GFDL or public domain, licence per file; the
  Foundation gives no warranty on the accuracy; attribution is required for CC BY/BY-SA; personality-right and trademark
  restrictions can still apply [V https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia]. Bulk
  and API rules were not read [R]. Per-file attribution does not fit a Subsonic cover endpoint, which is why option D is
  not recommended.
- **Last.fm**: excluded, unchanged.

## 5. Fallback: MusicBrainz plus Cover Art Archive

How it works (from the CAA API page https://musicbrainz.org/doc/Cover_Art_Archive/API, read via WebFetch, [V] unless marked): by a
MusicBrainz release or release-group id (mbid), `GET https://coverartarchive.org/release-group/<mbid>/front-500` (also
`front`, `front-250`, `front-1200`; also `/release/<mbid>/front[-size]`). The answer is an HTTP 307 redirect to the
image on archive.org; a missing size or image gives 404. The page says no rate limiting rules are in place for CAA, yet
lists 503 for exceeding a limit (inconsistent). MusicBrainz web service
(https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting, page dated 2012, "may change at any time"): source IP average
1 request per second; a descriptive `User-Agent` with a contact is REQUIRED (`App/<version> ( contact-url )`); a blank
Python-urllib or anonymous agent counts as anonymous; excess answers 503. Coverage: no figure was found [R]; it is partial,
better for popular Western albums.

Feasibility test: ONE request to the MusicBrainz web service (release-group search, descriptive User-Agent
`cowlic-cover-art-investigation/0.1 ( https://github.com/kolenkainc/cowlic )`) returned HTTP 503 and was NOT retried, to
stay polite. The CAA redirect was therefore not tested. Feasibility is NOT proven by observation; everything in this
section is from the documentation [R].

The ranking's optional mbid is the key; the author did not read the ranking file. A path without an mbid needs a
MusicBrainz search by artist and album name (fuzzy, 1 request per second, one more place for wrong matches). The
fallback adds a second network dependency and licence grey zone for partial coverage, so it is NOT in the recommended
phase 2 and stays a later ticket if the YouTube route has gaps.

## 6. Worker and schema impact (what phase 2 changes)

Read: `worker/src/index.ts` `cover`, `worker/src/library.ts`, `worker/src/stubs.ts`, `worker/migrations/0001_init.sql`,
`ingest/ingest.py`, `docs/worker-cicd.md` [V].

Current state [V]:
- `albums.cover_file_id` exists; `artists` has only `id, name`.
- `getCoverArt` calls `coverFileId(db, id)`, which only queries `albums` by id, then `proxyTelegramFile(..., "image/jpeg")`.
- `albumJson` sets `coverArt` to the album id when `cover_file_id` is set; `songJson` sets `coverArt` to the album id.
- Artist entries (`getArtists`, `getIndexes`, `getArtist`, `search3`) have `id, name, albumCount` only: no `coverArt`.
- `stubs.ts`: `getArtistInfo` and `getArtistInfo2` return an empty `artistInfo`/`artistInfo2` object, no image URLs;
  `getUser` has `coverArtRole: false`.
- `upsert_statements` upserts the album with `cover_file_id = COALESCE(albums.cover_file_id, excluded.cover_file_id)` (stored value first: a stored cover is never replaced by the SQL itself, #271), and
  `process_track` keeps a run-local `covers` dict `album_id -> file_id` loaded once at start from D1 [V `ingest.py:2092`].
  `Deps` has `upload_cover(song, dir)` that works from `song["cover_url"]`.
  The SQL alone protects a stored cover, the dict only saves the upload. A wrong cover is replaced by a manual D1 update
  (`UPDATE albums SET cover_file_id = ? WHERE id = ?`) by the owner; a dedicated replace tool is not needed (#271).

### (a) Album covers only (no schema or Worker change)

All in `ingest/`:
1. `match.find_match` must hand back the thumbnail base URL of the matched search result (extend `Candidate`, which holds
   only `video_id, title, artists, duration_s, score, mode, relaxed`; `process_track` calls `deps.find_match(song)` before
   the download, so the cover URL can be put on the song: `cover_url = base + "=w1200-h1200-l90-rj"` when `cover_url` is
   None). The nightly path with a Spotify `cover_url` stays unchanged (never overwrite an existing `cover_url`).
   **Wrong-cover guard in the new-track path (not only in the backfill).** `Candidate` has no album field today
   [V `ingest/match.py` `Candidate`], so the match must also carry the result's album name, album id, first artist id and
   artist names. Before `cover_url` is set, apply the guard: the result's album name must be similar to the song's
   `album_name` (the project's `similarity`), the song must have a real album name (`UNKNOWN_ALBUM` or an empty name never
   gets a cover from a match), and the result's artist names must match `primary_artist(song)`. This artist check is NEW, it
   is NOT something the matcher already does in the default duration mode: there `score` only adds `0.3 * a_sim` and the
   comment says "Artist only breaks ties; it must not veto" [V `ingest/match.py:~193`]; only `score_title_only` has a
   mandatory artist check. The new check gates ONLY the cover, never the audio match (the match and the download stay
   exactly as they are). It uses `artist_similarity` over the song's artist and its `transliterate_to_cyrillic` variant
   against the joined result artists and each single name, as `score_title_only` does, with a threshold to be chosen and
   tested in the ticket; `Candidate` carries the result's artist names for it. Without it, a cover act with the same
   title and duration could get its cover stored. Without it a wrong cover would be shared through `covers[al]` and kept forever by the COALESCE [V
   `ingest.py:540-549, 802-806`]. A song that fails the guard simply gets no cover (counted, logged without names).
2. `fetch_cover` hardening for URLs from the YouTube route [V today it only checks https and size, and `urlopen` follows
   redirects, `ingest.py:748-757`]: a host allowlist (`yt3.googleusercontent.com`, `lh3.googleusercontent.com`) checked
   on the first URL AND on every redirect target (or refuse redirects altogether), `Content-Type: image/jpeg`, the JPEG
   magic bytes `FF D8`, the 10 MB cap and https only kept. Failure keeps "WARN cover failed" and no cover. A hostile test
   (redirect to another host, non-JPEG body, oversized body) with a mutation check for each rule.
3. Tests offline with fakes; mutation checks; a nightly dry comparison with only Spotify sources must be byte-identical.
4. No Worker, no migration, no production change.

### (b) Artist photos too (owner wants them)

1. Migration `0003_artist_images.sql`: `ALTER TABLE artists ADD COLUMN image_file_id TEXT;` Additive. The lint treats
   `ADD COLUMN` as a warning, not idempotent (D1 tracks applied files in `d1_migrations`) [V `docs/worker-cicd.md`
   section 5 rule 3]. It reaches production through the sanctioned workflow (plan -> ship: migrate, then deploy) on merge,
   or by the owner; the Worker code must work with and without the column (rule 2): select the column only after it
   exists, or deploy the migration first (`needs: migrate` on `deploy`). Rollback in a comment: SQLite can
   `ALTER TABLE artists DROP COLUMN image_file_id` [R], leave the column otherwise. The ticket text says a production
   change must be an explicit line in the split ticket: it is, in ticket 3 of section 8.
2. Ingest: `upsert_statements` artist upsert gets `image_file_id` with the same COALESCE; a run-local `artist_images`
   dict loaded with `SELECT id, image_file_id FROM artists`; a new `Deps.upload_artist_image` (or a generalised cover
   upload) and one `get_artist(artists[0].id)` per NEW artist, thumbnail base + `=w800-h800-p-l90-rj`. The artist-id path
   is a no-op when the matched result has no artist id. **Artist-name guard:** `artists[0].id` of the match may belong to
   a different artist than `primary_artist(song)` (a feature credit, a cover act), so the photo is used only when the
   name returned by `get_artist` (or the result's first artist name) is similar to `primary_artist(song)`; otherwise no
   photo. The same hardened fetch as ticket 1 applies to the artist image.
   **Gate against a missing column:** the artist upsert sits in the shared D1 batch with the track insert, so if the
   code that writes `image_file_id` ran before migration 0003 is applied, EVERY track insert would fail. Therefore ticket
   4a is merged only after ticket 3 (the migration) is deployed and the column exists, and the code has a startup check
   (`PRAGMA table_info(artists)` or a one-row `SELECT image_file_id FROM artists LIMIT 1` through the same read-only D1 call
   that loads `artist_images`): when the column is missing the artist-image feature is switched off, the run logs one line
   and everything else runs as before [P].
3. Worker `library.ts`: `coverFileId` accepts an album id OR an artist id: `SELECT cover_file_id FROM albums WHERE id = ?`
   first, then `SELECT image_file_id FROM artists WHERE id = ?`. Album and artist ids are both 16-hex SHA-256 prefixes
   from different salted inputs (`stable_id("album", ...)`, `stable_id("artist", ...)`), so a collision is negligible but
   possible in theory; DECISION of this design: the artist's `coverArt` id is `ar-<id>` (prefix `ar-`, then the 16-hex
   artist id), and `getCoverArt` strips the prefix and reads `artists.image_file_id`; ids without the prefix keep reading
   `albums` exactly as today, so existing album `coverArt` values do not change [P, design choice]. `coverArt` on artist entries in `getArtists`/`getIndexes`/`getArtist`/`search3`, only when
   `image_file_id` is set (the artist SELECTs need the column; D1 queries must stay parameterized). Optionally
   `artistImageUrl` is not needed.
4. `smoke.mjs` and the CI smoke fixture get one artist with an image id.

How Symfonium asks for artist images: NOT verified [R]. The OpenSubsonic spec [V https://opensubsonic.netlify.app]:
`ArtistID3` has optional `coverArt` ("a cover art ID") and `artistImageUrl` ("a URL to an external image source");
`getCoverArt` takes the `coverArt` id and an optional `size`, and on OpenSubsonic servers `id` "refers to coverArt ID
only"; `getArtistInfo2` has `smallImageUrl`/`mediumImageUrl`/`largeImageUrl`. So the standard path is the `coverArt` field
of the artist entry plus `getCoverArt`, which phase 2 implements; Symfonium may additionally query `getArtistInfo2`
for image URLs (our stub returns an empty object). A URL there would have to carry the Subsonic credentials or a token,
so it is NOT recommended; if the real client does not show the photo from `coverArt`, the follow-up is to fill
`getArtistInfo2.largeImageUrl` with a `getCoverArt` link. The only real check is looking at Symfonium after
deploy, which is the acceptance step of ticket 6.

## 7. Backfill for albums (and artists) already stored without an image

- **Where the video id comes from.** D1 does NOT store the YouTube video id: `tracks` has `spotify_id` (the id, `lfm:...` for
  Last.fm songs), `album_id`, `title`, `duration_s`, sizes, `tg_file_id`, `tg_message_id`; `albums` has `name, year,
  cover_file_id`; `artists` has `name` [V `0001_init.sql`]. So it is found again: for one stored track per cover-less album
  run the same `find_match(ytm, title, artist, album, duration_s)` (1 to 3 search calls, same code as ingest, so the same
  accept rules) and take the thumbnail of the chosen result. No audio download. Cost per album about 1 to 3 search calls, plus
  one `get_artist` per cover-less artist [R, the count of such albums was not read from D1 here].
- **Mismatch guard.** The same guard as ticket 1 (shared code): accept the cover only if the result's `album.name` is
  similar (the project's `similarity`) to the stored album name and the result's artist matches the stored artist
  (the new cover-only artist check of 6(a) item 1, `artist_similarity` plus the Cyrillic variant; not the matcher's tie-break);
  otherwise skip the album and count it `album_mismatch` (it stays NULL and is reported). A stored album named
  `UNKNOWN_ALBUM` is never filled. Compilation or deluxe covers differ little for the same album, but a different album
  must not be used [P].
- **Command.** A new subcommand next to `rank/plan/run/status` of `ingest/bootstrap.py`, for instance `bootstrap.py covers`
  (with its dry count `covers --count`, below). It is NOT free through the wrapper: `scripts/run-bootstrap-local.sh` hard-codes the
  subcommand list `rank | plan | run | status` (line 55), the cap check (line 91) and the 21:10 guard (around line 194)
  are keyed on `run | plan` [V read]. So the ticket changes the wrapper, `scripts/test-run-bootstrap-local.sh` and
  `docs/bootstrap.md`. What it then reuses: the shared `ingest.lock`, the 21:10
  guard (`now + --max-minutes + 10 min + 60 s` must stay before the next 21:10), the required caps `--max-new N`
  (here: albums attempted) and `--max-minutes M`, the exit codes (75 locked, 78 usage or guard), and exit 3 on a sign-in
  challenge with no bypass [V `docs/bootstrap.md` section 4/5, read, not run]. **Dry count first:** `covers --count`
  (read-only: D1 reads only, no YouTube call, no Telegram, no write) prints how many albums (and artists) lack an image and
  how many are in the cooldown file, so the owner sees the size before the first real run. It needs no `--max-new` and
  `--max-minutes`, and the 21:10 guard does not apply to it (it ends in seconds). The wrapper DOES need a case entry
  for it: the wrapper takes the shared lock for every subcommand, `status` included [V `docs/bootstrap.md` line 158 and
  the lock header of the wrapper], so `covers --count` exits 75 while a session or the 21:10 run holds the lock; the
  wrapper's cap check must require the caps for `covers` without `--count` and reject them with `--count`. Pacing: at least 1.5 s between YouTube Music
  calls plus jitter, bounded retries only for transient errors, never for sign-in [P]; search calls are not the 300 videos
  per hour class of downloads but their limit is undocumented [R].
- **Resumable without a state file.** The work list is `SELECT ... FROM albums WHERE cover_file_id IS NULL` (artists:
  `image_file_id IS NULL`), so a stopped run resumes by itself. To avoid retrying the same hopeless albums every run, keep a
  small local JSON `cover-backfill.json` in `~/.local/state/cowlic` (album id to date and result `no_match`/`mismatch`)
  with a retry cooldown of N days [P]. Write: `UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL`
  (parameterized, idempotent; the album id is the one read from D1).
- **Interplay with the running bootstrap and the 21:10 timer.** The command takes the same lock; while a bootstrap session
  or the 21:10 run holds it, it exits 75 and does nothing. It must not run at the same time as a bootstrap session; run it
  between sessions. If an ingest run adds a cover for the same album meanwhile, `COALESCE` and `IS NULL` keep the first
  one; the cost is one orphan Telegram photo, harmless. The backfill does not read or write the Last.fm ranking or the
  bootstrap progress file.
- **Artist photos, as built (#253).** `covers --artists` (and `covers --artists --count`) is a MODE flag of `covers`, not a
  new subcommand, so the wrapper needs no logic (its tests only pin that the flag is forwarded and every rule of `covers`
  applies). It reuses the album machinery: the shared lock, the 21:10 guard, the caps, a cooldown file of its own
  (`artist-photo-backfill.json`) and exit 3. Per artist it calls the SAME functions as the ingest path: `find_match` on one
  stored track, `match.artist_id_from_match` (artist-name check with the Cyrillic bar, pseudo-artist denylist),
  `deps.artist_photo_url` (`get_artist`, suffix `=w800-h800-p-l90-rj`) and `deps.upload_artist_image` (the hardened fetch),
  then `UPDATE artists SET image_file_id = ? WHERE id = ? AND image_file_id IS NULL`. A database without the column is a
  clean skip. How to run it: `docs/bootstrap.md` section 11, "Artist photos".
- **Until it lands** the running bootstrap keeps adding cover-less albums; once (a) merges, new albums get covers and the
  backfill closes the old gap, so the order in section 8 is (a) first, backfill after.

## 8. Recommendation and phase 2 tickets

Recommendation: use YouTube Music thumbnails for both albums and artists (owner decision). Albums: the matched song's
thumbnail with the size suffix `=w1200-h1200-l90-rj`. Artists: `get_artist` thumbnail with `=w800-h800-p-l90-rj` (square
crop of the banner). Reuse the existing Telegram photo path and COALESCE. CAA is a documented fallback only. Every URL
suffix and the artist crop quality are checked on one example each only, so the Symfonium spot-check on real data is its
own ticket (6); the other tickets are verified offline (tests, `tsc`, smoke).

All tickets that touch `ingest/ingest.py` run serially. Order and sizes (S under half a day, M about a day):

| # | Ticket | Size | Notes |
|---|---|---|---|
| 1 | Album covers in ingest for new tracks: `Candidate` carries the result's album name/id, artist id and names; wrong-cover guard (album-name similarity, real album name, artist match) before `cover_url` is set; `fetch_cover` hardening (host allowlist on first URL and every redirect or no redirects, `image/jpeg`, `FF D8`, 10 MB, https); offline tests including a hostile test and a Latin vs Cyrillic artist-name test (the new cover-only artist check uses `artist_similarity` plus the `transliterate_to_cyrillic` variant and never gates the audio match), mutation checks, nightly byte-identical check | M | no Worker, no migration; touches `ingest.py` and `match.py`; the guard code is shared with 2 and 4b |
| 2 | Album backfill: `bootstrap.py covers` and `covers --count` (dry count), wrapper `scripts/run-bootstrap-local.sh` (subcommand list, cap check, 21:10 guard, `--count` case), `scripts/test-run-bootstrap-local.sh`, `docs/bootstrap.md`, cooldown file, exit 3 | M to L (Python plus bash plus runbook) | after 1; serial with other `ingest.py` work; not during a bootstrap session |
| 3 | Worker plus migration: `0003_artist_images.sql` (additive), `getCoverArt` accepts `ar-<id>`, `coverArt` = `ar-<id>` on artist entries (`getArtists`, `getIndexes`, `getArtist`, `search3`), smoke fixture, `tsc`, `smoke:ci`. Acceptance includes: an `ar-<id>` resolves to the artist image, a plain album id still resolves as before, an artist without an image has no `coverArt`. Production change: migration applied by the deploy workflow on merge or by the owner; the owner confirms in the ticket | M | independent of 1 and 2; must be deployed before 4a |
| 4a | Artist images, ingest side: `get_artist` per new artist, artist-name guard, artist upsert with COALESCE, `artist_images` dict, column-presence startup check (feature off when missing), hardened fetch, tests | M | after 1 and after 3 is deployed (otherwise every track insert fails): merge gate |
| 4b | Artist backfill: extend `covers` and `covers --count` to artists (`image_file_id IS NULL`), one search plus one `get_artist` per artist | M | after 2 and 4a |
| 5 | (optional) CAA fallback for albums without a YouTube match | S | only if the backfill report shows many `no_match` |
| 6 | Spot-check in Symfonium on real data (one album, one artist), record results on #245; if the artist photo does not show, fill `getArtistInfo2` | S | after 4a and 3 (and 2/4b for old data) |

Order: 1, then 2 and 3 in parallel (different files), then 4a, then 4b, then 6.

## 9. Risks

- ToS: automated thumbnail download is the same class as the existing ingest; accepted by the owner, not zero [R].
- The `=w1200-h1200-l90-rj` grammar is undocumented and may change; a failed fetch must mean "no cover".
- Wrong cover: the matched album can differ from the stored one (deluxe, compilation, a cover act) [V 2.1], and a
  Last.fm song can have a different or missing album name. Mitigated only if the guard lives in BOTH the new-track path
  (ticket 1: album-name similarity, real album name, artist match, `Candidate` extended) and the backfill (ticket 2);
  otherwise a wrong cover is shared through `covers[al]` and kept forever by the COALESCE. A wrong artist photo is
  guarded by the artist-name check in 4a. The similarity thresholds are untested on real data [R].
- A redirecting or hostile image URL: `urlopen` follows redirects today; ticket 1 closes it (section 6a point 2).
- Shipping the artist-image writer before migration 0003 is applied would fail every track insert; the merge gate and
  the column check in 4a cover it.
- The artist square crop is checked for one artist only; solo artists may crop badly [R].
- An artist with no `artists[0].id` or a pseudo-artist gets no photo.
- YouTube bot check: search calls were fine here (no challenge in 8 calls), but a sustained run can trigger it; the rule
  is unchanged: exit 3, stop, no bypass.
- Images are copyrighted: never commit them; the repo is public.
- Telegram photo recompression: `sendPhoto` may downscale and re-encode, so stored images may be lower resolution than
  requested [R].

## 10. Open questions for the owner

1. Albums only or also artists: ANSWERED, both (2026-10-11).
2. Is the mismatch guard (skip a cover when the matched album's name is not similar to the stored one) acceptable, or
   should any cover of the matched song be used? Default in this design: guard on.
3. The artist photo is a square crop of the YouTube banner (a band photo for The Kooks, one sample only). OK, or prefer
   no artist photo when the crop is doubtful? There is no automatic quality judge.
4. Production: ticket 3's migration reaches production through the merge workflow. Confirm that is the approved path
   for this one (the owner's decision of 2026-10-11 in `docs/worker-cicd.md` says yes) or that the owner will apply it.

## 11. What was and was not verified

Verified (section 2): eight ytmusicapi calls and two image downloads, all successful, one image viewed each (album 1200 px,
artist 800 px square); the repo code reads in section 6; the OpenSubsonic and ToS pages named above via WebFetch.
NOT verified: any Symfonium behaviour; the MusicBrainz and CAA calls (one request got 503, not retried); the real count of
cover-less albums and artists in D1 (no D1 read was made); the origin resolution of the source art; behaviour for any
artist other than The Kooks; any write to Telegram, D1 or the Worker (none was done).

## 12. Backfill second source, dry run and approved plan (#352)

Why: the first backfill (section 7, #250) left about 184 albums without a cover. The read-only investigation #350 found that the
guards are right: about 40% of the rejected hits are a recording by ANOTHER artist (cover, karaoke, namesake), and about half are
the right artist but another release (compilation, deluxe, the single). Loosening the album ratio, the edition words or the
artist check would store wrong covers, and a stored cover wins for good (`UPDATE ... WHERE cover_file_id IS NULL`, #271).
So nothing is loosened. Two narrow additions are made, and nothing is written without a dry run and the owner's review.

### 12.1 The rules (all of them keep the guards of section 7)

The default path (`covers --max-new N --max-minutes M`, no flag below) is unchanged: strict guard only.

1. **`track`**: the existing guard on the track-level match (album similarity >= 0.9, edition words, plural-only differences,
   numbers and Roman numerals, artist check with the Latin/Cyrillic bar 0.8). Only reported in a dry run.
2. **`normalised`** (`match.albums_match(..., narrow=True)`): the same guard after two narrow steps on the album names.
   (a) A trailing ` - Single` and a trailing standalone `[Explicit]` are dropped from both names (case-insensitive, repeated;
   `(Explicit)`, `Single - Name`, `Name-Single`, `Name - Single Remix` are NOT touched). (b) When one name is Latin only and the
   other Cyrillic only, the two names are also compared after the Latin -> Cyrillic transliteration table that the artist check
   already uses (a small deterministic table, no dependency). The table is lossy (`w` and `v` give the same letter), so it is
   never applied to two Latin names. After either step every guard of the default path still applies: only the name
   similarity (ratio >= 0.9) is computed on the transliterated text; the number and Roman-numeral check and the edition-word
   check run on the ORIGINAL words (the transliteration turns `ii` into `ии`), with both the Latin and a Cyrillic edition-word
   list (`ремикс`, `делюкс`, `сингл`, `живой`, `версия`, `издание`, ...) on both sides; the plural check also sees the
   transliterated `s` as `с`. Names longer than 200 characters never match (bounds the regex work). The artist check applies
   unchanged.
3. **`album_search`** (`bootstrap_covers.album_level`): for an album neither rule above accepts, one `ytmusicapi` search with
   filter `albums` for `<stored artist> <stored album>` (5 hits). A hit must pass the narrow album compare, the artist check
   (Cyrillic bar), the pseudo-artist denylist (`Various Artists`, `Unknown`, ...: stored artist and every credited artist), and a
   TRACKLIST CHECK: `get_album` of the hit must list a track with the same variant markers as the stored one (a live or remix
   track is not the stored track) and a title that meets the matcher's bar (similarity or relaxed similarity >= 0.85) together
   with a duration within +-3 s when both the stored track and the page's `duration_seconds` are known; when either duration
   is unknown the title alone must reach the stricter title-only bar 0.93 (numbered titles such as `Track A` / `Track B` score
   0.86). At most 3 `get_album`
   calls per album; the first hit that passes everything wins and its own thumbnail (`=w1200-h1200-l90-rj`) is the cover. A
   candidate that fails the tracklist check is rejected. Per-track thumbnails for singles are NOT used: for a single the song's
   thumbnail is mostly the art of a compilation or of another artist's release.

All YouTube Music calls (search, `get_album`) are paced (1.5 s plus jitter), retried only for transient errors, and a sign-in
challenge ends the run with exit 3: no cookies, proxies, PO-token tricks or client rotation (#62).

### 12.2 Dry run, review, approval, apply

```
ingest/.venv/bin/python ingest/bootstrap.py covers --dry-run --max-new N --max-minutes M --retry-days 0 --retry-failed-days 0
```

- Reads D1 and searches YouTube Music only. It never writes D1, never sends to Telegram and never changes
  `cover-backfill.json`. It takes the shared lock, needs both caps and has the 21:10 guard like the other modes. The albums of
  the first backfill rest in the cooldown (30 days), so a dry run over them needs `--retry-days 0`.
- It prints counts only: how many albums each rule would accept, how many the first source did not accept (by reason word), and
  how many every source rejected (by the album-level reason word: `no_hits`, `album_mismatch`, `artist_mismatch`,
  `pseudo_artist`, `no_browse_id`, `tracklist`, `no_thumbnail`).
- It writes the PLAN `cover-plan.json` in the private state directory (`~/.local/state/cowlic/`, file 0600, directory 0700):
  `{"version": 1, "approved": false, "created": "YYYY-MM-DD", "entries": {<album id>: {"rule", "browse_id", "cover_url"}}}`.
  Album ids are hashes; there is no artist, album or track name in the plan or in the output. `approved` is always written
  `false`; a new dry run overwrites the plan and resets it.
- The owner reviews the plan (the cover URLs can be looked at in a browser; the album ids can be resolved against D1 by the
  owner) and, if it is right, sets `"approved": true` BY HAND in the file (an editor; the value must be the JSON `true`, not
  `"true"` or `1`). Remove an entry to leave that album out. The file must not be group or world writable, must be owned by the user who runs the apply, and must be no
  more than 14 days old (its `created` date); otherwise the apply refuses and a new dry run is needed.
- The real step:

```
ingest/.venv/bin/python ingest/bootstrap.py covers --apply-plan ~/.local/state/cowlic/cover-plan.json --max-new N --max-minutes M --retry-days 0
```

  It REFUSES (exit 2, before D1, YouTube or Telegram are touched) unless the plan parses strictly and is approved. It then
  applies exactly the planned albums that are still cover-less (no new search, no new decision), under the same lock,
  21:10 guard, caps and cooldown; other albums are left alone. The hardened thumbnail fetch (host allowlist, JPEG check) and
  `UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL` are the same as in section 7, so a cover that
  exists wins. `--retry-days 0` is needed again only if those albums still rest in the cooldown. A YouTube sign-in challenge
  is exit 3 as everywhere.
- How to run: the commands above call `bootstrap.py` directly, which takes the lock itself. `scripts/run-bootstrap-local.sh
  covers ...` also works: it does not special-case `--dry-run` and `--apply-plan`, it forwards them (like every option except
  `--count`, `--r*` and `--m*`) to `bootstrap.py` and, as for any `covers` session, appends `--result-json`, applies its own
  caps check and 21:10 guard and ships the result to Grafana. The plan checks live in `bootstrap.py`, so they hold on both
  paths. This was read from the wrapper and its test, not run.

### 12.3 Honest expectation

On a random sample of 30 of the 184 albums, the normalisation accepted 3 and the album-level search added about 4 (its tracklist
check was designed, not measured). That is roughly 40 of 184 albums, with a wide interval. The rest stay without a cover by
design: recordings by another artist and releases that YouTube Music does not list under the stored name or only as a
compilation. For those the safe path is a cover chosen by the owner and a manual D1 update. The dry run prints the real
numbers; the owner decides after looking at them.

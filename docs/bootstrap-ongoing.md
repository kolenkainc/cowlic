# Bootstrap and ongoing: design of the two ingest processes

Contract for the implementation tickets #166-#178 (umbrella #102, milestone Release 3). Owner decision, 2026-10-10
(translated): "bootstrap = we parse the priorities from Last.fm ONCE, to fill the library; ongoing = the pipeline
every day at 21:10 that looks at which releases are missing and tops them up".

Written for the engineers (agents and the owner) who implement and operate it. Background: [architecture.md](architecture.md),
[local-ingest.md](local-ingest.md). Public repo: no secrets, no user names, no ids on this page (the key name
`LASTFM_API_KEY` only).

## 0. How to read this page

Every factual claim carries a tag:

- **[V]** verified: read in code or a document at the time of writing, with the file or ticket named. "Read" is
  not "run": nothing was executed against Last.fm, YouTube, D1 or Telegram for this page.
- **[R]** reported or inferred: not confirmed in a primary source, or an estimate. Do not build on it without
  checking; the ticket that needs it names how.
- **[P]** proposal or decision of this page: a design choice, not a fact. Implementers follow it unless a ticket
  or the owner (#165) says otherwise; "proposed" defaults may change when the owner answers #165.

State of the code at the time of writing: master has the Last.fm client and ranking (#148, `ingest/lastfm.py`),
the track enumeration (#149, `ingest/lastfm_tracks.py`), the `lfm:` id and cross-source dedupe (#150), and the
title-and-artist-only matcher mode (#151, `ingest/match.py`) [V]. The `lastfm_artists:` source and `run_lastfm`
(#152) are only in the open PR #162, not on master [V, `gh pr diff 162`]. Everything bootstrap- or
new-release-specific (#166-#174) does not exist yet. Where this page names a function of PR #162 it says so; check
it again after the merge.

## 1. Definitions and the rule that keeps the timer lean

| Term | Meaning |
|---|---|
| Ranking | The owner's top artists over a window (default 5 years), summed from weekly Last.fm charts: 1 call `user.getWeeklyChartList` plus one `user.getWeeklyArtistChart` per week, about 261 calls at 1 call/s, about 5 minutes [V, `ingest/lastfm.py` docstring and `MIN_INTERVAL = 1.0`] |
| Bootstrap | A ONE-TIME, manual, resumable job that works through the stored ranking artist by artist and fills the library. Started by the owner, capped per session, ends with `done: N artists, M tracks` |
| Ongoing | The daily 21:10 run: the playlists exactly as today, then a bounded check for missing releases of artists already in the library |
| Engine | The shared code path ranking -> tracks -> dedupe -> match -> download -> upload -> D1 (`run_lastfm` of #152 / PR #162 and the per-track pipeline `run` / `process_track` in `ingest/ingest.py` [V]) |
| Session | One invocation of `bootstrap.py run` |

### The rule

**The 21:10 run NEVER computes the Last.fm ranking and NEVER does a backfill.** It runs the playlists, then the
bounded new-release check, both under hard caps. Reasons: the ranking costs about 5 minutes and 261 calls per run
[V], and the first backfill is thousands of tracks at about a minute each [R, unmeasured, see #165 question 1],
while a normal night today is about 1-2 minutes [R, stated in #174; the 2026-10-10 21:10 run exited 0 per #102,
its duration is not recorded here].

How the rule is enforced, so it does not depend on anyone remembering it (all three are tickets, none exists yet):

1. The `lastfm_artists:` section of `sources.yml` is for the bootstrap engine only. The nightly wrapper refuses
   to start when `sources.yml` has an enabled `lastfm_artists:` section: one ERROR line, exit 78, before ingest
   starts (#174). [P]
2. The committed unit sets hard default caps (`COWLIC_INGEST_ARGS=--max-new 30 --max-minutes 30`,
   `TimeoutStartSec=1h`, #174). Today the unit has no caps and `TimeoutStartSec=2h` [V,
   `deploy/systemd/cowlic-ingest.service`]. Changing the repo copy has no effect on the owner's machine until the
   owner copies the unit (#175). [P]
3. The new-release phase is opt-in (`new_releases:` section, #172), has its own per-night caps (section 4), never
   reads the Last.fm ranking endpoints, and reads only the STORED ranking file. [P]

Bootstrap and ongoing never run at the same time: they share one lock (section 5).

## 2. Bootstrap contract

Proposal of the planner as in #163, adopted here unchanged ([P] unless a line says otherwise); any change a
ticket makes must be written back to this page.

### 2.1 Files and entry points

| File | Ticket | Purpose |
|---|---|---|
| `ingest/bootstrap.py` | #168 | The command, subcommands `rank`, `plan`, `run`, `status` |
| `ingest/bootstrap_ranking.py` | #167 | `compute`, `save`, `load`, `order` of the stored ranking (new module, stdlib only) |
| `scripts/run-bootstrap-local.sh` | #169 | The wrapper: shared lock, own log and status file |
| `docs/bootstrap.md` | #170 | Owner runbook (commands, troubleshooting). This page is the design, that one is the how-to |

The engine changes needed first are #166 (see 2.6). The Python entry point is not meant to be started by the
owner directly for `run`; the wrapper adds the lock, the log and the status file.

### 2.2 Subcommands

All options below are `--long` options of `bootstrap.py`; the wrapper passes them through.

| Subcommand | What it does | Writes | Network |
|---|---|---|---|
| `rank [--config FILE] [--refresh]` | Computes the ranking once (about 261 Last.fm calls) and stores it. If a ranking file exists it refuses, with one line, unless `--refresh`. A failed compute never overwrites a good file | `bootstrap-ranking.json` | Last.fm |
| `plan --max-new N --max-minutes M [--config FILE]` | Dry run: which artists the next session would reach under these caps and which matches it would attempt. Downloads nothing, uploads nothing, writes NO state file | nothing | Last.fm (enumeration), YouTube Music (searches, counted against `--max-new`), D1 read-only |
| `run --max-new N --max-minutes M [--config FILE] [--result-json FILE]` | One session. Both caps REQUIRED, no unlimited default: a missing or out-of-range value is exit 2 before any work | `bootstrap-state.json`, Telegram, D1 | Last.fm, YouTube, Telegram, D1 |
| `status` | Prints progress and the end state, exit 0, no network, no titles | nothing | none |

- `run` never computes the ranking. With no usable ranking file it exits non-zero with one clear line ("run rank
  first") [V, #168; the exit code 2 is [P]]. A missing, corrupt or unknown-version ranking file all mean "no
  ranking": never a crash, never a silent recompute (#167).
- `--max-new` and `--max-minutes` have the engine's meaning [V, local-ingest.md "Per-run limits"]: `--max-new` counts
  tracks attempted (new, too big, failed, and dry-run matches); tracks already stored, in cooldown or without a
  match are not counted. `--max-minutes` is wall clock: no new track is started after it, a download in progress
  finishes. Last.fm enumeration time counts as wall clock; the limits are checked before each artist is enumerated
  [V, `run_lastfm` in PR #162].
- Ranges: the wrapper validates like `parse_ingest_args` of the nightly wrapper [V]: integers, `--max-new`
  1..1000000, `--max-minutes` 1..1440. `bootstrap.py` applies the same ranges. `--pace-seconds` and the jitter
  keep the engine defaults (5 s plus 0..5 s) [V, local-ingest.md "Pacing and backoff"]; they are not new
  bootstrap options.
- `plan` is not free: it still counts YouTube searches against `--max-new` and may make Last.fm calls, so it is
  bounded by the same caps (#168). It never records an artist, so running it changes nothing for the next
  `plan` or `run`. [P]
- Subcommand-to-option map for the wrapper (#169): `run` requires both caps, `plan` requires both caps, `rank` and
  `status` take none.

### 2.3 Configuration

- A LOCAL file outside the repo, passed with `--config` (proposed location
  `${XDG_CONFIG_HOME:-$HOME/.config}/cowlic/bootstrap.yml`; owner question #165-6). Reason: the Last.fm user name
  must not be committed to this public repo. [P]
- Format: the `lastfm_artists:` section parser of #152 (strict two-space keys, no inline comments, unknown or
  repeated keys fail) reused as is [V, parser described in PR #162; check the final code after merge]. Keys used
  by bootstrap: `user` (2-15 of `[A-Za-z0-9_-]`), `years` (1..10, default 5), `max_artists` (1..10000 in the
  parser; the client refuses above 1000, default 100), `exclude` (artist names, compared with `match.normalize`).
  `rescan_days` is accepted by the shared parser and ignored by bootstrap (it never rescans); `enabled` has no
  meaning here. [P] on which keys are ignored; the parser facts are [V] from PR #162.
- Which subcommand needs what: `rank` needs `user`, `years`, `max_artists`. `run` and `plan` need only `exclude`
  from the file (the artists come from the stored ranking, enumeration is by artist name); `status` needs no
  config. So the user name is read once, by `rank`, and is never stored in any state or ranking file. [P]
- `LASTFM_API_KEY` is read from the environment or `.env` by the existing loader [V, `lastfm.api_key_from_env`,
  `tg_check.load_env`], never printed, never put in a file, a log or the config. `run` needs it too (enumeration
  calls). The wrapper never reads or sources `.env` [V, `run-ingest-local.sh`].
- `sources.yml` is NOT used by bootstrap. [P]

### 2.4 State files

Both live in the state directory `${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/` next to the log [V, same
directory as `ingest.log`], directory 0700, files 0600, every write atomic (temp file in the same directory +
fsync + rename; the existing `atomic_write_json` in `ingest/ingest.py` does exactly this [V]). Both hold ids,
hashes, dates and counts only: no Last.fm user name, no key, no URLs, no track titles. The nightly
`ingest-state.json` is never read or written by bootstrap, and bootstrap files are never read by the nightly run
except the ranking (section 4). The two files are gitignored by being outside the repo.

#### `bootstrap-ranking.json` (#167)

```json
{"version": 1, "computed_at": "2026-10-11T18:00:00Z", "years": 5, "max_artists": 100,
 "artists": [{"name": "Example Band", "playcount": 1234}]}
```

- Fields: `version`, `computed_at` (UTC), `years`, `max_artists`, and a list of `{name, playcount}` and nothing
  else [V, #167]. Names are cleaned with the existing `lastfm.clean_text`. `mbid` is NOT stored (#167 lists only
  name and playcount), so later enumeration is by name with `autocorrect`, as `artist_tracks(client, name, mbid=None)`
  supports [V, `ingest/lastfm_tracks.py`]. Whether dropping the mbid costs matches is [R, unmeasured].
- Order in the stored list is the priority order (2.5), already sorted; `load` validates types and sizes strictly
  and reports any problem as "no ranking".
- Written once by `rank`. `rank --refresh` replaces it; the old file stays when the new compute fails. [V, #167]
- The file is personal listening data (artist names and counts) in the owner's own state directory. Storing the
  DERIVED ordered list is the reading of the Last.fm terms this page relies on; the wording of the terms
  (cl. 4.3, caching follows the response headers) is [R, not confirmed, see #147, #165-3]. PR #162 does not
  cache at all because of this reading [V, PR #162]. If the owner does not accept (#165-3), `run` has to recompute
  the ranking at the start of every session (about 5 minutes, 261 calls); that changes #167 and #168, not the
  rest of this page.

#### `bootstrap-state.json` (#168)

```json
{"version": 1,
 "tracks": {"<song id>": {"status": "no_match", "retry_after": "2026-11-09"}},
 "artists": {"lfm:0123456789abcdef": "2026-10-12"},
 "bootstrap": {
   "ranking_computed_at": "2026-10-11T18:00:00Z",
   "sessions": 3,
   "artists": {"lfm:0123456789abcdef": {"status": "partial", "tracks_added": 12, "tracks_seen": 40,
                                         "failures": 0, "updated": "2026-10-12"}}
 }}
```

- The first three keys are the engine's own state format [V, `RunState` in `ingest/ingest.py`: `version`,
  `tracks` cooldowns `{status, retry_after}`, `artists` map of key -> last completed scan date; the key of a
  Last.fm artist is `lfm:` + 16 hex of the normalised name, `lastfm_artist_key` in PR #162]. #166 hands the
  engine this file as ITS state file, so cooldowns (30 days no_match, 3 days failed [V]) and the completed-scan
  dates behave exactly as in a nightly run, in a separate file.
- `bootstrap` is added by `bootstrap.py`. Per artist key: `status` in `pending | partial | done` (a key absent
  from the map is `pending`), counts, a `failures` counter for enumeration failures, the date of the last
  update. Pending/partial/done and counts are what `status` reads; nothing here is a name or a title. [P]
- Keys are hashes of the normalised name, not rank positions. A `rank --refresh` therefore keeps every status of
  an artist that is still in the new ranking; artists that dropped out stay in the file but are not counted;
  new artists are `pending`. `status` shows the rank position from the ranking file. [P]
- **Single-writer requirement, open design point for #166/#168 [P]:** `RunState.save()` rewrites only
  `version`, `tracks` and `artists` [V]. For the `bootstrap` object to survive an engine write, `RunState` must
  round-trip unknown top-level keys, or bootstrap and the engine must write through one owner. If #166 finds
  that too invasive for the byte-identical-behaviour rule of its acceptance criteria, the fallback is a sibling
  file `bootstrap-progress.json` with the same rules; the page and #168 are then updated together. Do not let
  two writers rewrite the same file independently.
- A corrupt or unknown-version `bootstrap-state.json`: the engine's `RunState.load` starts empty with a WARN and
  "never fails because of the state file" [V]. For bootstrap that would silently restart at the first artist (D1
  dedupe makes it correct but wasteful: re-enumeration of every done artist). Proposal [P]: `bootstrap.py`
  checks the file itself first, refuses to run with one clear line, and tells the owner to inspect or delete it.
  This is not in #168's acceptance criteria; the planner decides.

### 2.5 Priority policy

- Artist order: playcount over the window DESCENDING, ties by `match.normalize(name)` ASCENDING; deterministic for
  equal input (#167 tests it with shuffled input). `lastfm.top_artists` itself sorts ties by casefolded name [V];
  `bootstrap_ranking.order` makes the tie rule the one above and is the single place that defines the order.
- An artist is worked WHOLE, in rank order. A session cut by the cap, the time limit or a sign-in stop leaves
  the current artist `partial`; the next session resumes it first (it is the lowest-ranked unfinished artist,
  so rank order gives that for free).
- The list handed to the engine is the ranking minus `done` and minus `exclude`: the engine's `rescan_days`
  logic is never what decides whether an artist is worked. [P]
- Defaults awaiting the owner (#165): the per-artist track limit is NONE ("whole discography as #149
  enumerates it"), the order is as above, 100 artists. The only built-in bound per artist is the enumeration
  budget: at most 100 albums (`MAX_ALBUMS`) and 150 HTTP requests (`CALL_BUDGET`) [V, `ingest/lastfm_tracks.py`].
  An alternative order (recency-weighted) and a per-artist limit (top K albums) are open questions, not decided.

### 2.6 What the engine must offer (ticket #166, and a gap)

[V] `run_lastfm` in PR #162 takes `artists` (ranking order), an `enumerate_artist` callable, `stored_rows`, and
the state object; it records an artist only when its last track was handled (`state.record_artist`, never in a
dry run); it stops on cap or time before enumerating the next artist; it counts a failed enumeration and goes on;
it re-raises the YouTube sign-in stop after updating its counters; one `LastfmClient` per run with a budget of
ranking calls plus 150 per artist. #166 changes it to take an injected ranking and its own state file.

What bootstrap needs on top, for #166/#168 to settle. Items 1 and 2 are NOT in the text of #166 today; the planner
should add them:

1. **A per-artist progress hook**, called after each artist with the outcome (`done`, `cut` by cap/time/sign-in,
   `failed` enumeration) and the track counts of that artist, so `bootstrap.py` can write `partial` / `done`
   and the counters immediately. Without it the only record is the engine's `done` date, which cannot express
   `partial` or per-artist counts. [P]
2. **A client budget sized for the session**, not for `max_artists`: a session works at most as many artists as
   it reaches, `LastfmClient` has a hard `max_calls` (default `MAX_CALLS = 400`) [V]. Size it as
   150 x (artists in the planned list) or refuse to start; do not silently use 400. [P]
3. Resume cost: a `partial` artist is re-enumerated (up to about 150 requests, about 2.5 minutes at 1 call/s
   [V arithmetic from `CALL_BUDGET` and `MIN_INTERVAL`]), then the tracks already stored are skipped by the D1
   `existing` set and the cross-source dedupe, tracks in cooldown are skipped. It adds only what is missing; the
   cost is the enumeration, not repeated downloads. [V mechanism in PR #162; the real time is [R]]

### 2.7 Failure and resume behaviour

| Event | What happens | Recorded as |
|---|---|---|
| `--max-new` reached | Session ends normally, exit 0 [V, engine semantics] | current artist `partial`, stop reason `cap` |
| `--max-minutes` reached | No new track starts, a running download finishes, exit 0 | `partial`, stop reason `time` |
| YouTube sign-in challenge | The first one ends the whole session, exit 3. Never retried, never bypassed (#62): no cookies, proxies, PO-token tricks or client rotation [V, CLAUDE.md, local-ingest.md] | `partial`, stop reason `sign-in`. Wait and run again later; nothing else |
| One track fails (download, upload, D1) | Counted `failed`; cooldown 3 days [V]; the artist goes on. Exit 1 at the end like a failed track today [V] | in the engine state `tracks` map |
| No match for a track | Counted `no_match`, cooldown 30 days [V]. The track is NOT retried by bootstrap, and the nightly run never retries it (it does not enumerate Last.fm discographies) | `tracks` map |
| Artist enumeration fails (Last.fm error, budget exceeded, unknown artist) | Logged redacted, counted in `failed`, the other artists go on, retried next session [V, #168] | `failures` +1, status unchanged. A permanent cause (unknown artist, over the budget) would be retried every session, costing up to 150 calls each time: proposal [P], not in #168, show `failures >= 3` in `status` so the owner can add the artist to `exclude`; automatic parking is a decision for the planner |
| Process killed (SIGKILL, power loss) | Loses at most the current track. State is written after every change, atomically [V, `RunState` and `atomic_write_json`]. A kill between the Telegram upload and the D1 write leaves one orphan message in the channel and the track is uploaded again next session; the exception path deletes the orphan only when the process survives to run it [V, `process_track`; the kill case is [R], not tested] | the track is simply not stored |
| Another process holds the lock | The wrapper exits 75, touches nothing (section 5) | none |

`done` means "the artist's tracks were enumerated to the end and each was handled once", NOT "every track is
stored": `no_match`, `too_big` (mp3 over 20 MB, about 10 minutes at 256k [V, CLAUDE.md]) and cooled-down failed
tracks are part of a `done` artist. The result JSON counts them. Re-trying them is a later, explicit decision, not
something bootstrap does silently.

### 2.8 End state and output

- `status` prints one line, no titles, no names, exit 0:
  - every ranked artist (excluded ones left out) is `done`: `done: N artists, M tracks`
  - otherwise: `partial: X of N artists done, M tracks, next: <rank position>`
  - no ranking stored: one line saying so (still exit 0 for `status`, as it only reports). [P]
  `M` is the number of tracks bootstrap added (sum of `tracks_added`) [P]; tracks the library already held from
  playlists are not counted.
- `run --result-json FILE` writes counts only: added, skipped, no_match, too_big, failed, matched_title_only,
  `stop reason` (`done | cap | time | sign-in`, the engine's vocabulary [V, local-ingest.md "Why a run stopped"]).
- Log: no secret, no Last.fm user name, counts per artist, never track titles [V, #168]. The engine already logs
  artist names (`artist <name>: ...`) in the nightly style [V, PR #162]; names of artists are accepted in the log,
  titles are not. [R] whether the owner wants artist names out of the bootstrap log too: not asked yet.
- Exit codes: `bootstrap.py` 0 ok (a cap or time stop is NOT a failure), 1 some track or artist failed, 2 bad usage,
  missing caps, no ranking, bad config, 3 sign-in stop [V for 0/1/2/3 as ingest's codes, local-ingest.md "Exit
  codes"; the use of 2 for "no ranking" is [P]]. The wrapper adds 75 (locked) and 78 (prerequisite or
  config problem before anything ran, including missing or invalid caps) [V, #169 and the nightly wrapper].

## 3. What is shared and what is separate

| Thing | Bootstrap | Nightly 21:10 |
|---|---|---|
| Entry | `scripts/run-bootstrap-local.sh` -> `ingest/bootstrap.py` | `scripts/run-ingest-local.sh` -> `ingest/ingest.py` |
| Lock | `ingest.lock` | the same `ingest.lock` (section 5) |
| Log | `bootstrap.log` | `ingest.log` |
| Status file | `bootstrap-last-run.json` | `last-run.json` |
| State | `bootstrap-state.json`, `bootstrap-ranking.json` | `ingest-state.json` (+ the release-watch file, section 4) |
| Config | local file via `--config` | `sources.yml` in the repo |
| Grafana push | none in v1 | yes, `ship_grafana.py` |
| Last.fm ranking | computed once by `rank`, stored | never |
| Engine code | shared: dedupe, matcher, download, upload, D1, pacing, cooldowns, sign-in stop | same |
| D1 and Telegram | same library, same `existing` and cross-source dedupe against stored rows | same |
| Caps | required on the command line | defaults in the unit |

## 4. Ongoing contract (the 21:10 run)

### 4.1 Order and bounds

1. Playlists, exactly as today (a night without the `new_releases:` section is byte-identical in result JSON,
   stdout and state to today: #172 tests it against master).
2. The new-release phase (#172), after the playlists. It shares `--max-new`, `--max-minutes`, pacing, cooldowns
   and the first-sign-in stop (exit 3) with the playlist phase, so a heavy playlist night can leave it no budget:
   its artists are then `not_started` and stay due, and the next night picks them up. A failure inside the phase
   (a source error, one artist) is counted and logged, does not stop or fail the playlists, and changes the exit
   code only as a failed track does (class `partial`). [P, from #172]
3. The phase NEVER computes the Last.fm ranking, NEVER enumerates a whole discography, NEVER touches
   `bootstrap-state.json`. It reads the stored ranking file only to know the artist set. [P]

### 4.2 Which artists are "already in the library"

Proposed default: the artists of the stored ranking only (`bootstrap-ranking.json`). If no ranking is stored the
phase logs one line and does nothing. Alternative the owner may choose (#165-4): also every artist that has tracks
in D1 from the owner's playlists; D1 holds artist names in `artists` [V, `STORED_ROWS_SQL` joins
`tracks -> albums -> artists`], so the set is available without any new call, but an artist from a playlist is an
artist the owner did not rank, and the source link (4.4) would have to resolve it too. Not decided.

### 4.3 What "missing" means

A release of a known artist that is NEWER than that artist's stored watermark AND at least one of whose tracks
is not in D1 by the dedupe rule of #150: the same normalised artist, the same title key (live and explicit
variants stay distinct) and a duration within +-3 s, or artist + title key only when a side has no duration [V,
`dedupe.drop_stored`, `ingest/ingest.py` `drop_stored_lastfm`]. A release whose tracks are all stored only
advances the watermark. The Last.fm-style synthetic id (`lfm:` + 16 hex of artist + title, album not part of it
[V, `lastfm_tracks.song_id`]) is only the identity of a new row; "already stored" is always decided by the
artist + title (+ duration) rule, never by id equality, because the same song from a playlist has a Spotify id.

Known limitation, inherited from #150/#152 [V, the #152 coordinator notes]: a compilation track is stored under
the album artist, a Last.fm record compares on `artists[0]`, so such a track does not match and can be added a
second time. The nightly check shares this limitation.

### 4.4 Where releases come from: UNKNOWN until #164

The release source is not chosen. #164 has no result yet [V]. What is known:

- [V, #147] Last.fm `artist.getTopAlbums` is ordered by popularity and its documented fields have no release
  date; it cannot say "newest".
- [V, `ingest/lastfm_tracks.py`] Last.fm records built by #149 have `year: None` and `release_date: None`, so
  nothing in the existing records can drive a "newer than date X" watermark. A new-release source MUST provide a
  date, or the detector has nothing to compare.
- [R] `album.getInfo` may or may not carry a usable release date: unknown.
- [R] YouTube Music through `ytmusicapi` (`get_artist`, `get_artist_albums`, `get_album`): albums and singles
  with a year, date granularity unknown (possibly year only), unauthenticated behaviour, and the cost per artist
  all unverified.
- [R] MusicBrainz release groups with a first release date, about 1 request per second, needs an id mapping
  from the Last.fm artist: unverified.
- [V] `lastfm.READ_METHODS` is an allowlist of four methods (`user.getWeeklyChartList`,
  `user.getWeeklyArtistChart`, `artist.getTopAlbums`, `album.getInfo`). A source that needs another Last.fm
  method has to extend it deliberately.

Until #164 answers, everything in 4.5-4.6 about calls per artist is a budget shape, not a measurement. If #164
finds NO adequate source, #171/#172 are replaced by the smallest fallback the planner writes: no automatic
detection, a manual "refresh artist" command. This page is then updated by a small follow-up.

The link from a ranked artist to a source artist (name or mbid, or an id resolved once and stored) is part of
#164's answer. [R]

### 4.5 Per-artist check state

New file `new-releases-state.json` in the state directory (same rules as 2.4: 0600, atomic, ids and dates only),
owned by `ingest/new_releases.py` (#171). [P] This resolves the "next to the nightly one or inside it" choice of
#171: a SEPARATE file, so the nightly `RunState` (which rewrites only its own keys [V]) and the serial work on
`ingest/ingest.py` are not touched by the detector. Track cooldowns of tracks the phase attempts still go to
`ingest-state.json` through the shared engine, as for any track.

```json
{"version": 1, "artists": {"lfm:0123456789abcdef": {"checked_at": "2026-10-12", "watermark": "2026-09-30", "failures": 0}}}
```

- Keyed like the engine's `artists` map: `lfm:` + 16 hex of the normalised name, no plain names [V for the key
  shape in PR #162]. `checked_at` = last successful check; `watermark` = newest release date seen.
- First check of an artist uses a bounded look-back (default 90 days) so an old catalogue is never pulled by the
  nightly. An artist is due again after `recheck_days` (default 7). A FAILED check does not advance the
  watermark or `checked_at`; it is retried the next night. [P, from #171]

### 4.6 Per-night caps

Proposed defaults, not yet decided by the owner (#165-5):

| Cap | Value | Where |
|---|---|---|
| artists checked per night | 10 | `new_releases:` key `max_artists` |
| re-check interval | 7 days | `recheck_days` |
| look-back for a first check | 90 days | `lookback_days` |
| tracks added by the phase | 20 | `max_new` of the section; also bound by the shared `--max-new 30` of the unit |
| wall clock | 20 minutes for the phase [P: proposed by #165]; the whole run is capped by `--max-minutes 30` of the unit | owner question |
| source calls per artist | a hard budget, set when #164 is known | #171 |
| pacing | at least 1 s between source calls | #171 |

On a refusal or rate limit of the source the phase stops without retry loops [P, #171]; a sign-in challenge
during a download ends the whole run (exit 3) as today.

Worst case by the proposed numbers: 10 artists x (budget calls) x 1 s of checking [budget unknown, [R]] plus
20 tracks x about a minute of download [R, the figure local-ingest.md and the unit comment use] stays inside the
30-minute run cap; the cap, not the estimate, is what guarantees it.

### 4.7 Result JSON and metrics

- Result JSON gets an additive `new_releases` object, counts only: `due`, `checked`, `with_new`, `added`,
  `skipped`, `failed`, `not_started` (#172). Absent when the section is not enabled. [P]
- Metrics (#173, only where cheap): `ship_grafana.py` reads from `last-run.json` -> `result.new_releases` the
  allowlisted integers `checked`, `with_new`, `added`, `failed`; they appear in the `run_summary` line and as
  gauges `cowlic_ingest_new_releases_checked`, `_with_new`, `_added`, `_failed` (tags `job`, `host`; about 4 new
  series on top of about 15, far below the free tier of 10k series [V, local-ingest.md]). A `last-run.json`
  without the object ships exactly what it ships today. Fields that are not cheap are skipped. [P]

## 5. Shared resources

- **One lock.** Bootstrap and the 21:10 wrapper use the SAME `flock -n` on `ingest.lock` in the state directory.
  The nightly wrapper takes it on file descriptor 9 and exits 75 with one stderr line, touching neither the log
  nor `last-run.json`, when it is held [V, `scripts/run-ingest-local.sh`]; the lock descriptor is not inherited by
  children (`9>&-`) [V]. `run-bootstrap-local.sh` does the same (#169). So a bootstrap session started while the
  timer runs exits 75 at once, and a timer run that starts during a session exits 75 and does nothing that
  night. [V for the nightly side, [P] for the new wrapper]
- **Session timing.** Because of that, bootstrap sessions are started in the evening after the 21:10 run
  finished, with `--max-minutes` chosen so the session ends before the next 21:10 (proposed, #165-7). The timer
  has `RandomizedDelaySec=60` and `Persistent=true` [V, `deploy/systemd/cowlic-ingest.timer`]. What happens to a
  night lost to the lock is [R]: I did not verify whether systemd's catch-up applies when the service ran and
  exited 75 (the activation happened; my reading is that it does not). Either way the check is idempotent and
  the next night continues. `--max-minutes` is not a hard stop: a track in progress finishes [V], so leave
  margin of a few minutes. A hard guard (refuse to start when the next 21:10 is closer than `--max-minutes`) is
  not in any ticket; mention it to the owner if overlaps happen.
- **Separate log and status.** `bootstrap.log` (same rotation and redaction rules as `ingest.log`) and
  `bootstrap-last-run.json` (started_at, finished_at, exit_code, stop reason, counts; valid JSON always) (#169).
  The bootstrap wrapper never writes or touches `last-run.json` or `ingest.log`. [P]
- **No Grafana push from bootstrap in v1.** The wrapper never calls `ship_grafana.py`, so the dashboard
  "Last run" panels stay about the 21:10 run [V that the panels are built from `last-run.json` metrics, local-ingest.md
  "Dashboard"]. Bootstrap progress is read from `status`, the status file and the log. [P]
- **Not shared:** the ranking file and the bootstrap state; `ingest-state.json`; the release-watch file belongs
  to the nightly only.

## 6. Mapping of existing tickets

| Ticket | Role after the split |
|---|---|
| #148-#151 (Last.fm client and ranking, track enumeration, `lfm:` id and cross-source dedupe, title-only matcher) | Merged; the parts of the engine [V, on master] |
| #152 / PR #162 | THE ENGINE for both processes: bootstrap calls it with the stored ranking and its own state (after #166); the nightly never enables the `lastfm_artists:` section. The PR docs and text that describe enabling the section for the timer are superseded; the PR itself is kept as is (planner comment on #152). One sentence in its docs section is wanted: the nightly timer must not use the section, see this page |
| #153 report | Stays the match-rate gate before the first bootstrap session. Its runbook part about a first capped run through the nightly `sources.yml` is NOT written; #170 carries the dry run and first session steps through `bootstrap.py` (planner comment on #153) |
| #154 go/no-go | Stays the gate; #176 is blocked by it |
| #155 first capped run via the `sources.yml` section | Superseded by #176 (first bootstrap evening) if the owner agrees (#165-8); until then the section must not be enabled where the timer reads it |
| #110 terms stance | Owner decision, not a technical prerequisite |
| #163 (this page) | Contract for #166-#178 |
| #164 | Source for missing releases; blocks #171 |
| #165 | Owner decisions on section 7 |
| #166 | Engine takes an injected ranking and its own state file (plus 2.6 items); refactor only, byte-identical behaviour |
| #167 | `bootstrap_ranking.py` |
| #168 | `bootstrap.py` |
| #169 | `run-bootstrap-local.sh` and its test |
| #170 | `docs/bootstrap.md`; pointer line in `local-ingest.md` |
| #171 | `new_releases.py` (detector) |
| #172 | `new_releases:` section and the phase in `ingest.py` |
| #173 | Metrics and one dashboard panel |
| #174 | Lean 21:10: unit caps, `TimeoutStartSec=1h`, wrapper guard |
| #175 | Owner: install the lean unit, enable the check |
| #176, #177 | Owner: first bootstrap evening, sessions until `done` |
| #178 | Owner: one week of nightly results |

Order of work on `ingest/ingest.py`, never two agents at once: #152 -> #166 -> #168 -> #172. `docs/local-ingest.md`
is edited by #153, #170 (pointer) and #174: not together. Critical path: #152 -> #166 -> #168 -> #170 -> #176
(also needs #154) -> #177; second path #164 -> #171 -> #172 -> #175 -> #178. [V, milestone Release 3 description]

## 7. Open questions (one to one with #165)

Answers are not recorded yet: #165 has no owner comment [V at the time of writing]. The defaults below are marked
"proposed" everywhere on this page. When the owner answers, the planner updates this page with a small follow-up.

1. **Scale and per-artist limit.** Default: whole discography per artist as #149 enumerates it (bounded by 100
   albums and 150 calls), ranked by artist, 100 artists; re-decide after the first evening (#176) when real counts
   exist [R: thousands of tracks at about a minute each, unmeasured]. Alternative: top K albums per artist.
2. **Priority.** Default: 5-year playcount descending, ties by normalised name. Alternative: weight recent years
   more (how much).
3. **Ranking stored once.** Default: accept; only the derived list (names, counts, date) is stored locally and
   recomputed only on `--refresh`. The Last.fm caching terms (cl. 4.3) are [R, not confirmed]. If not accepted,
   every session recomputes the ranking (about 5 minutes, 261 calls).
4. **Which artists the nightly check covers.** Default: the artists of the stored ranking only. Alternative:
   also every artist with tracks in D1 from the owner's playlists.
5. **Nightly caps.** Default: check at most 10 due artists, add at most 20 tracks, stop after 20 minutes,
   re-check every 7 days, only releases newer than the last check. (The unit's run-wide `--max-new 30
   --max-minutes 30` in #174 is the outer bound.)
6. **Config file.** Default: a local file outside the repo, under the owner's config directory, passed with
   `--config`. Alternative: commit it (not recommended: the Last.fm user name would be public).
7. **Session timing against the shared lock.** Default: the owner starts sessions in the evening after the 21:10
   run finished, with `--max-minutes` so they end before the next 21:10; a session started while the timer runs
   exits 75.
8. **#155 replaced by #176** so that the nightly timer never picks up the Last.fm section.

Design points raised by this page that are not in #165 and need the planner (not the owner): the progress hook and
the session-sized client budget of 2.6; the single-writer fix for `bootstrap-state.json` (2.4); the refuse-on-corrupt
state rule (2.4); the handling of an artist whose enumeration keeps failing (2.7); the separate
`new-releases-state.json` (4.5).

## 8. What stays unchanged

- **D1 schema:** no migration. The `lfm:` id fits the existing `tracks.spotify_id TEXT UNIQUE` column, and
  `SONG_ID` already accepts exactly `lfm:[0-9a-f]{16}` next to Spotify ids [V, `ingest/ingest.py`;
  `worker/migrations/0001_init.sql` per the #147 research].
- **Worker and the Subsonic API**, **Telegram storage** (same bot, same channel, same 20 MB `getFile` limit
  [V, CLAUDE.md]), **the per-track pipeline** and its cooldowns.
- **YouTube handling:** no bypass of the bot check ever (#62). No cookies, proxies, PO-token tricks or client
  rotation, nothing stored anywhere. The first sign-in challenge stops the run (exit 3) in both processes.
- **Pacing and backoff:** engine defaults (5-10 s between downloads, backoff on 429/5xx, at most 1 Last.fm call
  per second) [V, local-ingest.md; `lastfm.MIN_INTERVAL`].
- **Last.fm use:** non-commercial, personal, names and playcounts only, no artwork or audio from Last.fm
  (cl. 3.1 and 5.1.8 are [R], reported by #147 and to be re-checked against the source before anyone relies on a
  clause number). Covers stay empty for Last.fm tracks [V, `lastfm_tracks.make_record`: `cover_url` None].
- **Out of scope:** liked songs (#47), playlist and favourite handling (#37, #38), alerts (#72), automatic
  bootstrap on the timer, any YouTube bot-check workaround.

## 9. Not verified when this page was written

- Nothing was run. No call to Last.fm, YouTube Music, MusicBrainz, D1, Telegram or the Subsonic Worker; no
  tests; no systemd command.
- Anything about the release source (section 4.4) beyond #147's findings: #164 has no result yet.
- Real call counts, run times and track counts per artist (everything marked "unmeasured"); the owner's profile
  privacy and the duration quality of Last.fm records are measured by #153/#154 and the first sessions.
- The wording of the Last.fm terms.
- PR #162 is read from its diff, not from master. If it changes before the merge, the places that cite it
  (`run_lastfm`, `lastfm_artist_key`, the section parser, the client budget) need a recheck.
- Whether systemd re-runs a catch-up after a run that exited 75.

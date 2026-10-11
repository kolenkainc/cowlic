# Bootstrap and ongoing: design of the two ingest processes

Contract for the implementation tickets #166-#178 (umbrella #102, milestone Release 3; since the regrooming of 2026-10-11 #171, #172 and #174 shipped in Release 3 (v3.0.0), and the open follow-ups #173, #175, #178 and #238 are in milestone Release 4 (v4.0.0)). Owner decision, 2026-10-10
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
(#152, PR #162, merged) are on master too [V, `ingest/ingest.py` on master: `run_lastfm`, `prepare_lastfm`,
`run_lastfm_phase`, `lastfm_artist_key`]. Everything bootstrap- or new-release-specific (#166-#174) does not exist
yet. Function names below are those of master when this page was last updated; #166 may rename them.

## 1. Definitions and the rule that keeps the timer lean

| Term | Meaning |
|---|---|
| Ranking | The owner's top artists over a window (default 5 years), summed from weekly Last.fm charts: 1 call `user.getWeeklyChartList` plus one `user.getWeeklyArtistChart` per week, about 261 calls at 1 call/s, about 5 minutes [V, `ingest/lastfm.py` docstring and `MIN_INTERVAL = 1.0`] |
| Bootstrap | A ONE-TIME, manual, resumable job that works through the stored ranking artist by artist and fills the library. Started by the coordinator or an agent through the wrapper (owner decision 2026-10-11, #165 question 7), capped per session, ends with `done: N artists, M tracks` |
| Ongoing | The daily 21:10 run: the playlists exactly as today, then a bounded check for missing releases of artists already in the library |
| Engine | The shared code path ranking -> tracks -> dedupe -> match -> download -> upload -> D1 (`run_lastfm` of #152 and the per-track pipeline `run` / `process_track` in `ingest/ingest.py` [V]) |
| Session | One invocation of `bootstrap.py run` |

### The rule

**The 21:10 run NEVER computes the Last.fm ranking and NEVER does a backfill.** It runs the playlists, then the
bounded new-release check, both under hard caps. Reasons: the ranking costs about 5 minutes and 261 calls per run
[V], and the first backfill is thousands of tracks at about a minute each [R, unmeasured, see #165 question 1],
while a normal night today is about 1-2 minutes [R, stated in #174; the 2026-10-10 21:10 run exited 0 per #102,
its duration is not recorded here].

How the rule is enforced, so it does not depend on anyone remembering it (all three are tickets, none exists yet):

1. The `lastfm_artists:` section of `sources.yml` is for the bootstrap engine only. The nightly wrapper refuses
   to start when `sources.yml` has a `lastfm_artists:` section: one ERROR line, exit 78, before ingest starts
   (#174). The guard is a plain whole-line grep, so it also trips on a section with `enabled: false` (only
   commented-out lines pass); that is intended and the message should say so. [P, from #174]
2. The committed unit sets hard default caps (`COWLIC_INGEST_ARGS=--max-new 30 --max-minutes 30`,
   `TimeoutStartSec=1h`, #174). Today the unit has no caps and `TimeoutStartSec=2h` [V,
   `deploy/systemd/cowlic-ingest.service`]. Changing the repo copy has no effect on the owner's machine until the
   owner copies the unit (#175). [P]
3. The new-release phase is opt-in (`new_releases:` section, #172), has its own per-night caps (section 4), never
   reads the Last.fm ranking endpoints, and reads only the STORED ranking file. [P]

Bootstrap and ongoing never run at the same time: they share one lock (section 5).

## 2. Bootstrap contract

Proposal of the planner as in #163, with the changes this page makes marked ([P] unless a line says otherwise);
any change a ticket makes must be written back to this page.

### 2.1 Files and entry points

| File | Ticket | Purpose |
|---|---|---|
| `ingest/bootstrap.py` | #168 | The command, subcommands `rank`, `plan`, `run`, `status` |
| `ingest/bootstrap_ranking.py` | #167 | `compute`, `save`, `load`, `order` of the stored ranking (new module, stdlib only) |
| `scripts/run-bootstrap-local.sh` | #169 | The wrapper: shared lock, own log and status file |
| `docs/bootstrap.md` | #170 | Runbook for the coordinator and the owner (commands, troubleshooting). This page is the design, that one is the how-to |

The engine changes needed first are #166 (see 2.6). The wrapper adds the log and the status file. The lock is
NOT only the wrapper's job: `bootstrap.py run` (and `rank --refresh`, which writes the ranking) takes the same
`flock` on `ingest.lock` itself, so that starting the Python entry point directly cannot overlap the timer or
another session. Otherwise two processes each load the D1 `existing` set once and can upload the same track
twice. [P, requirement for #168 and #169]

Chosen mechanism, one only [P]: the wrapper takes the lock exactly as the nightly wrapper does (`exec 9>"$LOCK"`,
`flock -n 9`, exit 75 before anything else [V, `scripts/run-ingest-local.sh` on master]), starts the child with
`9>&-` (the descriptor is not inherited [V]) and sets the environment variable `COWLIC_LOCK_HELD=1` for the child.
`bootstrap.py` takes the flock itself, with `flock.LOCK_EX | LOCK_NB` on `ingest.lock`, only when that variable is
absent (a direct call), exit 75 if it is held. The variable is a convenience against accidental direct calls,
not a security boundary. Why not "the wrapper locks and the child locks again": `flock` locks belong to the open
file description, so a second `open()` of the same file conflicts with the first even in a child process; a child
that opens the lock file while the wrapper holds it would always get 75 (standard `flock(2)` semantics, not run
here). #168 and #169 test both paths: via the wrapper (no double lock) and direct (takes the lock, exit 75 when
held).

### 2.2 Subcommands

All options below are `--long` options of `bootstrap.py`; the wrapper passes them through.

| Subcommand | What it does | Writes | Network |
|---|---|---|---|
| `rank [--config FILE] [--refresh]` | Computes the ranking once (about 261 Last.fm calls) and stores it. If a ranking file exists it refuses, with one line, unless `--refresh`. A failed compute never overwrites a good file | `bootstrap-ranking.json` | Last.fm |
| `plan --max-new N --max-minutes M [--config FILE]` | Dry run: which artists the next session would reach under these caps and which matches it would attempt. Downloads nothing, uploads nothing, writes NO state file | nothing | Last.fm (enumeration), YouTube Music (searches, counted against `--max-new`), D1 read-only |
| `run --max-new N --max-minutes M [--config FILE] [--result-json FILE]` | One session. Both caps REQUIRED, no unlimited default: a missing or out-of-range value is exit 2 before any work | `bootstrap-progress.json`, `bootstrap-state.json`, Telegram, D1 | Last.fm, YouTube, Telegram, D1 |
| `status` | Prints progress and the end state, exit 0, no network, no titles | nothing | none |

- `run` never computes the ranking. With no usable ranking file it exits non-zero with one clear line ("run rank
  first") [V, #168; the exit code 2 is [P]]. A missing, corrupt or unknown-version ranking file all mean "no
  ranking": never a crash, never a silent recompute (#167).
- `--max-new` and `--max-minutes` have the engine's meaning [V, local-ingest.md "Per-run limits"]: `--max-new` counts
  tracks attempted (new, too big, failed, and dry-run matches); tracks already stored, in cooldown or without a
  match are not counted. `--max-minutes` is wall clock: no new track is started after it, a download in progress
  finishes. Last.fm enumeration time counts as wall clock; the limits are checked before each artist is enumerated
  [V, `run_lastfm` on master].
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
  repeated keys fail) reused as is [V, `ingest/ingest.py` on master]. Keys used
  by bootstrap: `user` (2-15 of `[A-Za-z0-9_-]`), `years` (1..10, default 5), `max_artists` (1..10000 in the
  parser; the client refuses above 1000, default 100), `exclude` (artist names, compared with `match.normalize`).
  `rescan_days` is accepted by the shared parser, but bootstrap does NOT ignore it in the engine: `run_lastfm`
  applies `state.artist_due(key, cfg.rescan_days)` to every artist handed in [V, master],
  so bootstrap must pass `rescan_days=0` (on master `artist_due` is then always true: `days >= 0` [V,
  `RunState.artist_due`]). The value in the file has no meaning for bootstrap; `enabled` has none either. [P]
  The parser facts are [V] on master.
- `rank` sizes its Last.fm client for the ranking like `prepare_lastfm` does (`1 + years * 53 + 4` plus the
  per-artist budget [V, `prepare_lastfm` on master]); the default `MAX_CALLS = 400` [V] is below that for `years`
  above about 7. Either size it or cap `years`. [P]
- Which subcommand needs what: `rank` needs `user`, `years`, `max_artists`. `run` and `plan` need only `exclude`
  from the file (the artists come from the stored ranking, enumeration is by artist name); `status` needs no
  config. So the user name is read once, by `rank`, and is never stored in any state or ranking file. [P]
- `LASTFM_API_KEY` is read from the environment or `.env` by the existing loader [V, `lastfm.api_key_from_env`,
  `tg_check.load_env`], never printed, never put in a file, a log or the config. `run` needs it too (enumeration
  calls). The wrapper never reads or sources `.env` [V, `run-ingest-local.sh`].
- `sources.yml` is NOT used by bootstrap. [P]

### 2.4 State files

All three live in the state directory `${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/` next to the log [V, same
directory as `ingest.log`], directory 0700, files 0600, every write atomic (temp file in the same directory +
fsync + rename; the existing `atomic_write_json` in `ingest/ingest.py` does exactly this [V]). Both hold ids,
hashes, dates and counts only (the ranking file also holds artist names and playcounts, see below): no Last.fm
user name, no key, no URLs, no track titles. The nightly `ingest-state.json` is never read or written by
bootstrap, and bootstrap files are never read by the nightly run except the ranking (section 4); the nightly
may read the ranking while `rank --refresh` replaces it, which is safe because the replacement is an atomic
rename. The files are gitignored by being outside the repo. Files: `bootstrap-ranking.json` (what to work
through), `bootstrap-state.json` (the engine's own state: cooldowns, side-effect scan dates) and
`bootstrap-progress.json` (the single truth about each artist's status). [P]

#### `bootstrap-ranking.json` (#167)

```json
{"version": 1, "computed_at": "2026-10-11T18:00:00Z", "years": 5, "max_artists": 100,
 "artists": [{"name": "Example Band", "playcount": 1234,
              "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da"}]}
```

- Fields: `version`, `computed_at` (UTC), `years`, `max_artists`, and a list of `{name, playcount, mbid?}` and
  nothing else [V, #167]. Names are cleaned with the existing `lastfm.clean_text`. `mbid` is stored when Last.fm
  gave one: optional, validated with the existing `_MBID` pattern and stored lowercase, omitted (never an empty
  string) when absent or invalid; `load` refuses any other form. Decision recorded in #167: `top_artists` returns
  the mbid and `artist_tracks(client, name, mbid)` accepts it [V, `ingest/lastfm.py`,
  `ingest/lastfm_tracks.py`], and dropping it lets an ambiguous name enumerate the wrong artist and add wrong
  tracks to the library. [V, `ingest/bootstrap_ranking.py`]
- Order in the stored list is the priority order (2.5), already sorted; `load` validates types and sizes strictly
  and reports any problem as "no ranking".
- Written once by `rank`. `rank --refresh` replaces it; the old file stays when the new compute fails. [V, #167]
- The file is personal listening data (artist names and counts) in the owner's own state directory. Storing the
  DERIVED ordered list is the reading of the Last.fm terms this page relies on; the wording of the terms
  (cl. 4.3, caching follows the response headers) is [R, not confirmed, see #147, #165-3]. `run_lastfm_phase`
  does not cache at all because of this reading [V, master: it reads the ranking at every run]. If the owner does not accept (#165-3), `run` has to recompute
  the ranking at the start of every session (about 5 minutes, 261 calls); that changes #167 and #168. It also
  removes the artist set of the nightly phase (section 4.2 default): questions 3 and 4 of #165 are COUPLED. With
  no stored ranking the nightly phase uses the other artist set, the artists in D1 (a read it already needs),
  and nothing else. [P]

#### `bootstrap-state.json` (#168)

```json
{"version": 1,
 "tracks": {"<song id>": {"status": "no_match", "retry_after": "2026-11-09"}},
 "artists": {"lfm:0123456789abcdef": "2026-10-12"}}
```

- This file is the engine's own state format, nothing else [V, `RunState` in `ingest/ingest.py`: `version`,
  `tracks` cooldowns `{status, retry_after}`, `artists` map of key -> last completed scan date; the key of a
  Last.fm artist is `lfm:` + 16 hex of the normalised name, `lastfm_artist_key` on master]. #166 hands the
  engine this file as ITS state file, so cooldowns (30 days no_match, 3 days failed [V]) behave exactly as in a
  nightly run, in a separate file. Bootstrap never writes into it. The `artists` map here is a SIDE EFFECT of the
  engine (`record_artist`); bootstrap never reads it to decide anything. `RunState.load` on master accepts `lfm:`
  artist keys next to Spotify ids (`LASTFM_ARTIST_KEY` [V, `ingest/ingest.py` on master]).

#### `bootstrap-progress.json` (#168): the single truth about artists

Decided now, not left to #166 [P]: a SIBLING file, written only by `bootstrap.py`. Round-tripping an extra key
through `RunState` would touch `ingest/ingest.py`, which #166 must keep byte-identical in behaviour, and the
engine saves after every track, so a shared file would have two writers.

```json
{"version": 1, "ranking_computed_at": "2026-10-11T18:00:00Z", "sessions": 3,
 "artists": {"lfm:0123456789abcdef": {"status": "partial", "enum": "ok", "truncated": false,
                                       "tracks_added": 12, "tracks_seen": 40, "failures": 0,
                                       "updated": "2026-10-12"}}}
```

- Per artist key: `status` in `pending | partial | done | parked` (a key absent from the map is `pending`);
  `enum`, the enumeration outcome the hook receives: `ok | unknown_artist | no_albums | unusable_artist` (the
  statuses of `ArtistTracks` [V, `ingest/lastfm_tracks.py`]); `truncated` (more than `MAX_ALBUMS` albums existed
  [V field `albums_truncated`]); counts; `failures`; date of the last update. Nothing here is a name or a title.
- **This is the single source of truth for "artist done".** Bootstrap passes `rescan_days=0` to the engine and
  hands it only artists that are not `done` or `parked` and not excluded. The hook writes the status. A crash
  between the engine's own `record_artist` write and the hook's write therefore cannot leave an artist
  `partial` forever: on the next session the artist is handed in again, the engine (with `rescan_days=0`) works
  it, finds everything stored, and the hook marks it `done`.
- Keys are hashes of the normalised name, not rank positions. A `rank --refresh` keeps every status of an artist
  still in the new ranking; artists that dropped out stay in the file but are not counted; new ones are
  `pending`. `status` shows the rank position from the ranking file.
- **Unknown and zero-track artists are NOT silently `done`.** On master `artist_tracks` does not raise for an
  unknown artist (Last.fm error 6) or one without albums: it returns an empty `ArtistTracks` with status
  `UNKNOWN_ARTIST`, `NO_ALBUMS` or `UNUSABLE_ARTIST` [V, `ingest/lastfm_tracks.py` `_enumerate`], and in
  `run_lastfm` such an artist goes through `run([])`, is counted `scanned` and recorded by `record_artist` [V,
  `ingest/ingest.py` on master]. The status never leaves the engine today: `real_lastfm_enumerator` returns only
  `.records`, so #166 must pass the status (and `truncated`) through to the hook. An artist whose name is empty
  after `match.normalize` is skipped and counted (`lr.unusable`) by master's `run_lastfm` before enumeration [V];
  bootstrap records it as `unusable_artist` itself. Without the hook a top-ranked artist spelled differently on Last.fm would be reported as a
  clean `done` with no tracks. The hook must receive and store `enum` and `truncated` (a requirement for
  #166, whose "enumeration fails" test does not cover this case), and `status` counts them separately (2.8).
  Only `ArtistBudgetExceeded` and other `LastfmError`s raise and are enumeration FAILURES (2.7).
- **Corrupt state policy [P], belongs in #168's acceptance criteria.** The engine's `RunState.load` starts
  empty with a WARN for a corrupt, over-50 MB or unknown-version file and silently drops malformed entries [V],
  which for bootstrap would restart at the first artist and re-enumerate every done artist (about 2.5 minutes
  each, up to about 4 hours of Last.fm calls for 100 artists [V arithmetic from `CALL_BUDGET`, `MIN_INTERVAL`],
  all counted against the session caps). So `bootstrap.py` checks BOTH `bootstrap-progress.json` and
  `bootstrap-state.json` itself, stricter than the engine: it refuses to run (exit 2, one line) when a file is
  unreadable, too big, of an unknown version, or when ANY entry would be dropped. `status` then prints
  `state file unusable: <reason>`. Recovery is an explicit `run --reset-state` / `status`-suggested step that
  MOVES the file aside to `<name>.bak` (never deletes, never overwrites an existing `.bak`: numbered suffix) and
  starts empty.

### 2.5 Priority policy

- Artist order: playcount over the window DESCENDING, ties by `match.normalize(name)` ASCENDING; deterministic for
  equal input (#167 tests it with shuffled input). `lastfm.top_artists` itself sorts ties by casefolded name [V];
  `bootstrap_ranking.order` makes the tie rule the one above and is the single place that defines the order.
- An artist is worked WHOLE, in rank order. A session cut by the cap, the time limit or a sign-in stop leaves
  the current artist `partial`; the next session resumes it first (it is the lowest-ranked unfinished artist,
  so rank order gives that for free).
- The list handed to the engine is the ranking minus `done`, `parked` and `exclude`, with `rescan_days=0`: the
  engine's own scan dates are never what decides whether an artist is worked (2.4). [P]
- Defaults awaiting the owner (#165): the per-artist track limit is NONE ("whole discography as #149
  enumerates it"), the order is as above, 100 artists. The only built-in bound per artist is the enumeration
  budget: at most 100 albums (`MAX_ALBUMS`) and 150 HTTP requests (`CALL_BUDGET`) [V, `ingest/lastfm_tracks.py`].
  An alternative order (recency-weighted) and a per-artist limit (top K albums) are open questions, not decided.

### 2.6 What the engine must offer (ticket #166, and a gap)

[V, `ingest/ingest.py` on master] `run_lastfm` takes `artists` (ranking order), an `enumerate_artist` callable,
`stored_rows`, and the state object; it records an artist only when its last track was handled
(`state.record_artist`, never in a dry run); it stops on cap or time before enumerating the next artist; it counts
a failed enumeration and goes on; it re-raises the YouTube sign-in stop after updating its counters. The ranking
is read inside `run_lastfm_phase` (after the playlists, through a `DeadlineClient` so `--max-minutes` also bounds
the ranking) with one `LastfmClient` per run created by `prepare_lastfm` with a budget of ranking calls plus 150
per artist. #166 changes this to take an injected ranking and its own state file.

What bootstrap needs on top, for #166/#168 to settle. Items 1, 2 and 3 are NOT in the text of #166 today; the
planner should add them:

1. **A per-artist progress hook**, called after each artist with the outcome (`done`, `cut` by cap/time/sign-in,
   `failed` enumeration), the enumeration status (`ok | unknown_artist | no_albums | unusable_artist`, and
   `truncated`) and the track counts of that artist, so `bootstrap.py` can write `partial` / `done` /
   `parked` into `bootstrap-progress.json` immediately. Without it the only record is the engine's scan date,
   which cannot express `partial`, per-artist counts or an empty enumeration. [P]
2. **`rescan_days=0` and the progress file as the single truth** (2.4): #166 must not let the engine's scan
   date decide which artists are worked. [P]
3. **A client budget sized for the session**, not for `max_artists`: a session works at most as many artists as
   it reaches, `LastfmClient` has a hard `max_calls` (default `MAX_CALLS = 400`) [V]. Size it as
   150 x (artists in the planned list) or refuse to start; do not silently use 400. [P]
4. Resume cost: a `partial` artist is re-enumerated (up to about 150 requests, about 2.5 minutes at 1 call/s
   [V arithmetic from `CALL_BUDGET` and `MIN_INTERVAL`]), then the tracks already stored are skipped by the D1
   `existing` set and the cross-source dedupe, tracks in cooldown are skipped. It adds only what is missing; the
   cost is the enumeration, not repeated downloads. [V mechanism on master; the real time is [R]]

### 2.7 Failure and resume behaviour

| Event | What happens | Recorded as |
|---|---|---|
| `--max-new` reached | Session ends normally, exit 0 [V, engine semantics] | current artist `partial`, stop reason `cap` |
| `--max-minutes` reached | No new track starts, a running download finishes, exit 0 | `partial`, stop reason `time` |
| YouTube sign-in challenge | The first one ends the whole session, exit 3. Never retried, never bypassed (#62): no cookies, proxies, PO-token tricks or client rotation [V, CLAUDE.md, local-ingest.md] | `partial`, stop reason `sign-in`. Wait and run again later; nothing else |
| One track fails (download, upload, D1) | Counted `failed`; cooldown 3 days [V]; the artist goes on. Exit 1 at the end like a failed track today [V] | in the engine state `tracks` map |
| No match for a track | Counted `no_match`, cooldown 30 days [V]. The track is NOT retried by bootstrap, and the nightly run never retries it (it does not enumerate Last.fm discographies) | `tracks` map |
| Artist enumeration FAILS (`ArtistBudgetExceeded` or another `LastfmError` such as a refused key or a malformed answer; NOT an unknown artist, see the next row) | Logged redacted, counted in `failed`, the other artists go on, retried next session [V, #168; the exceptions are [V, `ingest/lastfm_tracks.py`]] | `failures` +1, status unchanged. After `N` consecutive failures (default 3 [P]) the artist becomes `parked`: skipped by `run` and `plan`, counted and reported by `status`, never retried automatically, so a permanent cause cannot eat each session (a retry costs up to 150 calls, about 2.5 minutes, before any track). Un-park: an explicit `run --unpark` for all parked artists (they become `pending`, `failures` reset), or add the artist to `exclude`. This is a requirement for #168's tests (an end state needs it) [P] |
| Unknown artist, no albums, unusable name | NOT an exception: `artist_tracks` returns an empty result with a status [V]. The artist has nothing to download | stored in `enum` (`unknown_artist`, `no_albums`, `unusable_artist`), the artist ends `done` with 0 tracks, and `status` counts these separately (2.8). `exclude` only REMOVES an artist from the work list, it cannot fix a spelling, so the owner accepts the gap or the name is handled by a later change (for example storing `mbid`, 2.4). The status must reach the hook: `real_lastfm_enumerator` returns only `.records` on master [V], so #166 passes the status through [P] |
| Process killed (SIGKILL, power loss) | Loses at most the current track. State is written after every change, atomically [V, `RunState` and `atomic_write_json`]. A kill between the Telegram upload and the D1 write leaves one orphan message in the channel and the track is uploaded again next session; the exception path deletes the orphan only when the process survives to run it [V, `process_track`; the kill case is [R], not tested] | the track is simply not stored |
| Another process holds the lock | The wrapper (or `bootstrap.py` itself) exits 75, touches nothing (section 5) | none |

`done` means "the artist's tracks were enumerated to the end and each was handled once", NOT "every track is
stored" (and for `unknown_artist` / `no_albums` it means nothing was found to enumerate): `no_match`, `too_big` (mp3 over 20 MB, about 10 minutes at 256k [V, CLAUDE.md]) and cooled-down failed
tracks are part of a `done` artist. The result JSON counts them. Re-trying them is a later, explicit decision, not
something bootstrap does silently.

### 2.8 End state and output

- `status` prints one line, no titles, no names, exit 0:
  - every ranked artist (excluded ones left out) is `done` or `parked`:
    `done: N artists, M tracks, K parked` (the `, K parked` part only when K > 0, and the owner should read
    it as "done except K artists that need a decision")
  - otherwise: `partial: X of N artists done, M tracks, next: <rank position>` (plus `, K parked` when K > 0)
  - no ranking stored, or an unusable state file: one line saying so (still exit 0 for `status`, as it only
    reports; the refusal exit 2 belongs to `run`). [P]
  - a second line, always, when any apply: `empty: E artists with no tracks found (unknown or no albums),
    truncated: T artists` so a clean `done` is never mistaken for a complete library [P].
  `M` is the number of tracks bootstrap added (sum of `tracks_added`) [P]; tracks the library already held from
  playlists are not counted.
- `run --result-json FILE` writes counts only: added, skipped, no_match, too_big, failed, matched_title_only,
  `stop reason` (`done | cap | time | sign-in`, the engine's vocabulary [V, local-ingest.md "Why a run stopped"]).
- Log: what bootstrap itself writes (status, session summaries, progress file, result JSON) holds no secret, no Last.fm
  user name, no artist names and no track titles. The shared engine does write names and titles into the log and the
  terminal: `artist <name>: ...`, `ingested: <artist> - <title>`, `dry-run would ingest: <artist> - <title>`,
  `ERROR <artist> - <title>` [V, `run_lastfm` and `run` on master]. `bootstrap.log` is local, 0600 and never shipped (the
  wrapper's `ship_grafana --filter` only redacts secrets). A title filter would be a separate ticket (see 2.9). [R] whether
  the owner wants artist names out of the bootstrap log too: not asked yet.
- Exit codes: `bootstrap.py` 0 ok (a cap or time stop is NOT a failure), 1 some track or artist failed, 2 bad usage,
  missing caps, no ranking, bad config, 3 sign-in stop [V for 0/1/2/3 as ingest's codes, local-ingest.md "Exit
  codes"; the use of 2 for "no ranking" is [P]]. The wrapper adds 75 (locked) and 78 (prerequisite or
  config problem before anything ran, including missing or invalid caps) [V, #169 and the nightly wrapper].

### 2.9 As implemented in `ingest/bootstrap.py` (#168)

Where the code settled a point this page left open (the code and `ingest/test_bootstrap.py` are the reference):

- `run --reset-state` moves BOTH `bootstrap-progress.json` and `bootstrap-state.json` aside (numbered `.bak`, `.bak.1`,
  ... never overwritten) and starts empty; the ranking and the nightly `ingest-state.json` are not touched. It happens
  after the lock, the 21:10 guard, the config, ranking and environment checks, so a refused start moves nothing.
- `status` also accepts `--config FILE` (only `exclude` is read, so excluded artists are left out of N); without it,
  or without the default config file, nothing is excluded. A bad config prints `config unusable: ...`, exit 0.
- `plan` runs the same strict state check as `run` (exit 2), takes no lock and is not guarded by the 21:10 rule; `run`
  and `rank` (with or without `--refresh`) take the lock themselves unless the real environment has `COWLIC_LOCK_HELD=1`
  (exactly `1`; read before any `.env` load).
- The shared parser validates the whole config file: `enabled: false` or a missing `user` is refused as a bad config
  (exit 2) even for `run`, `plan` and `status`, which do not use those keys; a file with playlists or a
  `followed_artists:` section is refused too.
- `tracks_added` of an artist accumulates over sessions; `tracks_seen` is the largest number of tracks handled in one
  pass. The progress file is written after each artist (not each track), so a process killed inside an artist loses
  the `tracks_added` count of that artist (the tracks are in D1 and are skipped next time; `M` of `status` can undercount).
- `--result-json` holds `stop_reason` (`done | cap | time | sign-in`, `null` when the session failed before it ran; `done` on the nothing-to-do path), `exit_code`, `counts`
  (added, skipped, no_match, too_big, failed, matched_title_only, cooldown, not_attempted) and `artists` (planned,
  completed, cut, failed, not_started, parked, unusable). It is written only once the session got past the lock, the guard
  and the checks (no file for exit 2, 75 or 78).
- The engine's per-track log lines (`ingested: <artist> - <title>`) are those of `run()` and are not changed or filtered;
  `bootstrap.log` is never shipped. What bootstrap itself writes (status, summaries, progress, result) holds no names
  and no titles.

## 3. What is shared and what is separate

| Thing | Bootstrap | Nightly 21:10 |
|---|---|---|
| Entry | `scripts/run-bootstrap-local.sh` -> `ingest/bootstrap.py` | `scripts/run-ingest-local.sh` -> `ingest/ingest.py` |
| Lock | `ingest.lock` | the same `ingest.lock` (section 5) |
| Log | `bootstrap.log` | `ingest.log` |
| Status file | `bootstrap-last-run.json` | `last-run.json` |
| State | `bootstrap-state.json`, `bootstrap-progress.json`, `bootstrap-ranking.json` | `ingest-state.json` (+ the new-release state file, section 4.5; reads `bootstrap-ranking.json` only) |
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
   the bootstrap files. It reads the stored ranking file only to know the artist set. [P]

### 4.2 Which artists are "already in the library"

Proposed default: the artists of the stored ranking only (`bootstrap-ranking.json`). Alternative the owner may
choose (#165-4): also every artist that has tracks in D1 from the owner's playlists. Coupling with #165-3: if the
owner does not accept storing the ranking, there is no ranking file for the nightly to read, and the artist set
is then the D1 artists (the only other set that exists without a Last.fm call); with no ranking AND no D1 artists
the phase logs one line and does nothing. [P]

The check compares releases against D1 album names per artist, so the phase needs one more read-only,
parameterless D1 query once per run (artist name, album name, year of every album, in the pattern of
`STORED_ROWS_SQL` [V]); `albums` has `artist_id`, `name`, `year INTEGER` [V, `worker/migrations/0001_init.sql`].
The per-artist filtering is done in Python, never by string-building SQL. [P]

### 4.3 What "missing" means

Decided by #164 (see 4.4), replacing the "newer than a watermark date" model of the first version of this page and
of the bodies of #165-5, #171 and #172, which are now stale (list at the end of section 7). The source gives the
YEAR only, never a day, so a date watermark cannot work.

1. Per artist, list the newest releases (4.4). A release is MISSING when no album of that artist in D1 has the
   same normalised title: a SET DIFFERENCE on (artist, normalised title), with the same normalisation as
   `match.normalize` (NFKC, lower case, punctuation collapsed to spaces [V, `ingest/lastfm_tracks.py` docstring]),
   because the probe showed `Hail to the Thief (Live Recordings 2003-2009)` on YouTube Music against
   `...: Live Recordings 2003–2009` elsewhere [V, #164 comment, observed live].
2. The year is only a skip filter and a tie-break: a release clearly older than the artist's newest stored album
   year may be skipped without a `get_album` call; a release with no year stays a candidate; the year is never
   the identity. [P]
3. For each missing release (not in the evaluated set of 4.5): `get_album` -> records with title, artists and
   `duration_seconds` -> `dedupe.drop_stored` against the stored rows (same normalised artist, same title key,
   duration within +-3 s, or artist + title only when a side has no duration [V, `ingest/dedupe.py`]) -> the
   normal per-track pipeline. "Already stored" is always decided by that rule, never by id equality, because the
   same song from a playlist has a Spotify id.
4. A re-run finds nothing: a landed release is in D1 (its album name), a release that did not land is in the
   evaluated set (4.5).

**Record ids.** `SONG_ID` accepts only a 22-character Spotify id or `lfm:[0-9a-f]{16}` [V, `ingest/ingest.py`
L49 on master]. Records built from YouTube Music need one of those forms. Proposal [P]: reuse
`lastfm_tracks.song_id(artist, title)` (`lfm:` + 16 hex of normalised artist + title, album not part of it [V]),
so the same song reached through bootstrap or through the nightly check gets the same id and the D1
`spotify_id UNIQUE` column and the `existing` set dedupe it by id as well as by the artist + title rule. The
prefix then means "synthetic artist + title id", not "from Last.fm". The alternative, a new prefix (for example
`ytm:`), needs `SONG_ID` and `RunState` changed in `ingest/ingest.py`: not proposed. #171 decides and tests it.

Known limitation, inherited from #150/#152 [V, the #152 coordinator notes]: a compilation track is stored under
the album artist, a record compares on `artists[0]`, so such a track would not match and could be added a second
time. The nightly check (#171) avoids that by dropping, and counting in its log line, every track of a release whose
listed artists do not include the checked artist; a release that holds only such tracks counts as handled. A release the owner already has under a different title (deluxe,
remaster, regional edition) looks "missing"; the evaluated set (4.5) bounds the cost, the dedupe rule bounds the
duplicates.

### 4.4 Where releases come from: decided in #164

Source: YouTube Music through `ytmusicapi`, `get_artist_albums(order="Recency")`, FIRST page only, for albums and
singles, unauthenticated. No MusicBrainz, no Last.fm release data in the nightly path. [V, #164 research and the
live probe on the owner's laptop on 2026-10-10, two artists measured: Radiohead and Taylor Swift]

- [V, observed live, 2 artists] `order="Recency"` returns newest first with a year for albums and singles. For one
  artist the DEFAULT order started with the 2021 album and MISSED a 2025 release that Recency put first, so the
  default order is not usable. The year is a year only, no day. Singles carry type `Single`, albums `Album`.
  It worked from the owner's IP without a login, like `search`.
- [V, read in ytmusicapi 1.12.3 source, #164] `get_artist`, `get_artist_albums` and `get_album` do not call
  `_check_auth`; with `order` set `get_artist_albums` costs 2 requests per kind (page, then the sort
  continuation). The backend is unofficial and undocumented, no published limit or terms [R]. The same bot-check
  family as #62 may apply; the rule is the same: stop on the first block or HTTP 429/403, no retry loop beyond
  the engine's backoff, no bypass.
- [V, observed live] MusicBrainz is out: HTTP 503 after about 25 calls at 1 call per 2 s from the owner's IP,
  5-6 paging calls per artist just to list release groups, and it lists items YouTube Music does not have
  (B-sides, future-dated entries), which would give false "missing" signals.
- [V] Last.fm `artist.getTopAlbums` is ordered by popularity, no date field; Last.fm records of #149 have
  `year`/`release_date` None [V, `ingest/lastfm_tracks.py`]. Not a detector.
- Artist link: from a ranked (or D1) artist name to a YouTube Music artist by one `search(filter="artists")`,
  accepting an EXACT normalised-name match only, skipping the artist when it is ambiguous; the browse id is
  cached (4.5). [P, from #164]

Not verified (carried over from #164): only 2 of 5 planned artists were measured; whether every artist's singles
carry a year; first-page size beyond the 5 items shown; behaviour for two artists with the same name; the YouTube
Music terms. If the source proves unusable, the fallback is the owner-question fallback of section 7: no automatic
detection, a manual `refresh <artist>` command that does steps 1-3 of 4.3 for one named artist.

### 4.5 Per-artist check state

New file `new-releases-state.json` in the state directory (same rules as 2.4: 0600, atomic, ids and dates only),
owned by `ingest/new_releases.py` (#171). [P] A SEPARATE file, so the nightly `RunState` (which rewrites only its
own keys [V]) and the serial work on `ingest/ingest.py` are not touched by the detector. Track cooldowns of tracks
the phase attempts still go to `ingest-state.json` through the shared engine, as for any track.

```json
{"version": 1,
 "artists": {"lfm:0123456789abcdef": {"browse_id": "<YouTube Music artist id>", "checked_at": "2026-10-12",
                                       "failures": 0, "evaluated": ["0a1b2c3d4e5f6071:2025", "99aa88bb77cc66dd:"]}}}
```

- Key: `lfm:` + 16 hex of the normalised name, as the engine's `artists` map [V for the key shape, `lastfm_artist_key` on master];
  no plain names. `browse_id`: the cached YouTube Music artist id, a public id, no secret; absent until
  resolved, left unset when the artist was ambiguous (then `failures` is not raised: it is "unresolvable", and
  the artist is skipped without calls). `checked_at`: last SUCCESSFUL check; an artist is due again after
  `recheck_days` (default 7). A FAILED check changes neither `checked_at` nor `evaluated` and raises `failures`;
  it is retried the next night. No `watermark`, no `lookback_days`: they cannot work with year-only data.
- **`evaluated`: a bounded set of already-evaluated release keys per artist** (key = 16 hex of the normalised
  release title + `:` + year or empty; cap 50, oldest evicted). Without it a release that never lands (all
  tracks `no_match`, `too_big` or in cooldown, or an album the owner has under another title) would be
  "missing" every night and cost a `get_album` call every time, because cooldowns save the searches, not the
  `get_album`. A release is added when EVERY one of its tracks was handled: stored, already present,
  dropped by dedupe, `no_match` or `too_big`. A release with a `failed` track, or one cut by the cap or the time
  limit, is NOT added, so it is retried (the failed track through its 3-day cooldown). [P]

### 4.6 Per-night caps

Proposed defaults, not yet decided by the owner (#165-5):

| Cap | Value | Where |
|---|---|---|
| artists checked per night | 10 | `new_releases:` key `max_artists` |
| re-check interval | 7 days | `recheck_days` |
| tracks added by the phase | 20 | `max_new` of the section; also bound by the shared `--max-new 30` of the unit |
| wall clock | 20 minutes for the phase [P: proposed by #165]; the whole run is capped by `--max-minutes 30` of the unit | owner question |
| requests per artist per night | check: 1 `get_artist` (the state caches only `browse_id`, not the section params, so it is needed every night) + 2 `get_artist_albums` (albums, singles) at up to 2 requests each (page, sort continuation, [V per the #164 reading of the ytmusicapi source]) = at most 5 requests. Extra: 1 `search` the first night only, 1 `get_album` per missing release. Hard budget in #171: 5 + 1 per `get_album` allowed by `max_new`, + 1 on the first night | #171 |
| pacing | at least 1 s between source calls | #171 |

No `lookback_days` row: the first check of an artist has no date to look back to; the first page of Recency and
the evaluated set bound it (an artist whose newest releases are all missing from D1 on the first check adds at
most `max_new` tracks that night; the rest is picked up by later nights as their keys stay unevaluated). [P]

On a refusal or rate limit of the source the phase stops without retry loops [P, #171]; a sign-in challenge
during a download ends the whole run (exit 3) as today.

Worst case by the proposed numbers, checking only: 10 artists x 5 requests x 1 s = 50 s a night, plus 10
searches (10 s) on the first night, plus 1 `get_album` per missing release (at most 20 by `max_new`), all inside
the 20-minute phase cap; downloads: 20 tracks x
about a minute [R, the figure local-ingest.md and the unit comment use]. The real request counts are unmeasured
[R]; the caps, not the estimate, are what guarantee it stays under the 30-minute run cap.

### 4.7 Logging: no names in the shipped log

The nightly wrapper ships `ingest.log` lines to Grafana Cloud Loki [V, local-ingest.md "Shipping the run log"].
The engine's `run_lastfm` logs `artist <name>: ...` lines for the Last.fm source [V, master], which is fine for
the never-shipped `bootstrap.log` but would send the owner's taste to a third party from the nightly. Requirement
for #171/#172 [P]: the new-release phase writes counts and hashed keys only to `ingest.log` (due, checked,
with_new, added, failed; the 16-hex key), no artist and no release names; it must not reuse the artist-name log
lines of the Last.fm engine. If the owner wants the phase to log names, that is a question for #165, not a
default.

### 4.8 Result JSON and metrics

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
  children (`9>&-`) [V]. `run-bootstrap-local.sh` does the same (#169), and `bootstrap.py run` takes the lock
  itself too (2.1), so calling the Python entry point directly cannot overlap. So a bootstrap session started
  while the timer runs exits 75 at once, and a timer run that starts during a session exits 75 and does nothing
  that night. [V for the nightly side, [P] for the new wrapper and `bootstrap.py`]
- **Session timing.** Because of that, bootstrap sessions (started by the coordinator or an agent, not by the owner:
  owner decision 2026-10-11, #165-7) start once the feature is merged and reviewed, but never while the 21:10 run
  holds the lock, with `--max-minutes` chosen so the session ends before the next 21:10 (the wrapper refuses a
  session that would cross it). The timer
  has `RandomizedDelaySec=60` and `Persistent=true` [V, `deploy/systemd/cowlic-ingest.timer`]. **This page does
  NOT promise a catch-up** for a night lost to the lock: whether systemd re-fires a run whose activation already
  happened and exited 75 is [R] and unverified (the reading is that `Persistent=true` covers elapses missed
  while the timer was inactive, so no catch-up). The consequence, stated plainly: **a lost night leaves NO local
  record.** The locked wrapper touches neither `ingest.log` nor `last-run.json` [V] and pushes nothing to
  Grafana, and the dashboard only flags more than 14 days without a run [V, local-ingest.md "Dashboard"], so the
  owner would not learn that 21:10 was skipped. The check itself is idempotent and the next night continues.
- **Guard against overlap (implemented in #169, wrapper and `bootstrap.py run`).** `--max-minutes` goes up to 1440 [V] and is not a hard stop:
  a track in progress finishes [V]. So `run-bootstrap-local.sh` (and `bootstrap.py run`) refuse to start, exit
  78 with one line, when `now + --max-minutes + a margin (default 10 minutes)` crosses the next 21:10 local
  time (plus the timer's up to 60 s random delay [V]). The runbook (#170) states the same rule in words.
- **Separate log and status.** `bootstrap.log` (same rotation and redaction rules as `ingest.log`) and
  `bootstrap-last-run.json` (started_at, finished_at, exit_code, stop reason, counts; valid JSON always) (#169).
  The bootstrap wrapper never writes or touches `last-run.json` or `ingest.log`. [P]
- **No Grafana push from bootstrap in v1.** The wrapper never calls `ship_grafana.py`, so the dashboard
  "Last run" panels stay about the 21:10 run [V that the panels are built from `last-run.json` metrics, local-ingest.md
  "Dashboard"]. Bootstrap progress is read from `status`, the status file and the log. [P]
- **Not shared:** the bootstrap state and progress files; `ingest-state.json`; the new-release state file
  belongs to the nightly only. **One-way shared:** the ranking file is written by bootstrap (`rank`) and READ by
  the nightly phase to get its artist set; the atomic rename makes a read during `rank --refresh` safe. The
  nightly never writes it. If the owner does not accept storing it (#165-3) see 4.2.

## 6. Mapping of existing tickets

| Ticket | Role after the split |
|---|---|
| #148-#151 (Last.fm client and ranking, track enumeration, `lfm:` id and cross-source dedupe, title-only matcher) | Merged; the parts of the engine [V, on master] |
| #152 / PR #162 (merged) | THE ENGINE for both processes: bootstrap calls it with the stored ranking and its own state (after #166); the nightly never enables the `lastfm_artists:` section. The merged docs section in `docs/local-ingest.md` that describes enabling the section for the timer is superseded by this page (#170 and #174 add the note and the guard) |
| #153 report | Stays the match-rate gate before the first bootstrap session. Its runbook part about a first capped run through the nightly `sources.yml` is NOT written; #170 carries the dry run and first session steps through `bootstrap.py` (planner comment on #153) |
| #154 go/no-go | Stays the gate; #176 is blocked by it |
| #155 first capped run via the `sources.yml` section | Superseded by #176 (first bootstrap session; the owner agreed, #165-8); the section must never be enabled where the timer reads it |
| #110 terms stance | Owner decision, not a technical prerequisite |
| #163 (this page) | Contract for #166-#178 |
| #164 | DONE (research and live probe): the source is YouTube Music `get_artist_albums(order="Recency")`, set difference by (artist, normalised title), no date watermark (4.3-4.4). Blocks #171, whose body (`since`, `watermark`, `lookback_days`) is now stale |
| #165 | Owner decisions on section 7 |
| #166 | Engine takes an injected ranking and its own state file (plus 2.6 items); refactor only, byte-identical behaviour |
| #167 | `bootstrap_ranking.py` |
| #168 | `bootstrap.py` |
| #169 | `run-bootstrap-local.sh` and its test |
| #170 | `docs/bootstrap.md`; pointer line in `local-ingest.md` |
| #171 | `new_releases.py` (detector); contract of this page: 4.3-4.6 (set difference, `browse_id`, evaluated set, no watermark) |
| #172 | `new_releases:` section and the phase in `ingest.py`; drop `lookback_days`, no names in the shipped log (4.7) |
| #173 | Metrics and one dashboard panel |
| #174 | Lean 21:10: unit caps, `TimeoutStartSec=1h`, wrapper guard |
| #175 | Owner: install the lean unit, enable the check |
| #176, #177 | Coordinator/agents: first bootstrap session, sessions until `done` (owner decision 2026-10-11, #165-7; the owner may interrupt at any time) |
| #178 | Owner: one week of nightly results |

Order of work on `ingest/ingest.py`, never two agents at once: #152 -> #166 -> #168 -> #172. `docs/local-ingest.md`
is edited by #153, #170 (pointer) and #174: not together. Critical path: #152 -> #166 -> #168 -> #170 -> #176
(also needs #154) -> #177; second path #164 -> #171 -> #172 -> #175 -> #178 (#173, #175, #178 and #238 are in Release 4 since 2026-10-11; #171, #172 and #174 shipped in Release 3). [V, milestone Release 3 description, as of 2026-10-10]

## 7. Open questions (one to one with #165)

Owner answers of 2026-10-11 (#165): the defaults below are accepted for all questions, except question 5 (the check
runs EVERY night after the 21:10 playlists, bounded, #172) and question 7 (changed, see there). Entries not marked
"Decided" still read as the original proposal.

1. **Scale and per-artist limit.** Default: whole discography per artist as #149 enumerates it (bounded by 100
   albums and 150 calls), ranked by artist, 100 artists; re-decide after the first bootstrap session (#176) when real counts
   exist [R: thousands of tracks at about a minute each, unmeasured]. Alternative: top K albums per artist.
2. **Priority.** Default: 5-year playcount descending, ties by normalised name. Alternative: weight recent years
   more (how much).
3. **Ranking stored once.** Default: accept; only the derived list (names, counts, date) is stored locally and
   recomputed only on `--refresh`. The Last.fm caching terms (cl. 4.3) are [R, not confirmed]. If not accepted,
   every session recomputes the ranking (about 5 minutes, 261 calls), and the nightly phase has no ranking file
   to read (coupled with question 4: it then uses the D1 artists, 4.2).
4. **Which artists the nightly check covers.** Default: the artists of the stored ranking only. Alternative:
   also every artist with tracks in D1 from the owner's playlists.
5. **Nightly caps.** Default: check at most 10 due artists, add at most 20 tracks, stop after 20 minutes,
   re-check every 7 days. The proposal in #165 also says "only releases newer than the last check": that part is
   STALE, the release source gives a year only; the rule is now "releases not in D1 and not yet evaluated"
   (4.3, 4.5). (The unit's run-wide `--max-new 30 --max-minutes 30` in #174 is the outer bound.)
6. **Config file.** Default: a local file outside the repo, under the owner's config directory, passed with
   `--config`. Alternative: commit it (not recommended: the Last.fm user name would be public).
7. **Session timing against the shared lock.** Decided 2026-10-11 (#165, changed answer): the owner does NOT
   start sessions; the coordinator or an agent starts them through the wrapper as soon as the feature is merged and
   reviewed, with `--max-minutes` so they end before the next 21:10 (the wrapper refuses a session that would cross
   it); a session started while the timer runs exits 75. The first session is small, after a `plan`.
8. **#155 replaced by #176** so that the nightly timer never picks up the Last.fm section.

### Raised by #164, NOT in #165

The research comment of #164 ends with three owner questions that are not among the eight above. Defaults are
proposed, nothing is decided:

9. **Is year precision enough?** The source gives a year only; "missing" = a release title not in the library
   among the newest releases. Default: yes. Day precision would need MusicBrainz as a second source, which the
   probe showed to be a poor fit (503 after about 25 calls, false "missing" signals).
10. **Is the fallback acceptable if the source proves unusable?** Default: yes, a manual `refresh <artist>`
    command and no automatic detection.
11. **Do singles and EPs count as missing releases, or albums only?** Default: both (the probe read albums and
    singles; singles add `get_artist_albums` calls and library growth).

Also for the owner, if wanted (not a default): whether the nightly phase may log artist or release names to
the shipped `ingest.log` (4.7). Default: no.

### Design points for the planner, not the owner

The progress hook, `rescan_days=0` and the enumeration status of 2.4 and 2.6; the session-sized client budget
(2.6) and `rank` client sizing (2.3); the sibling progress file, the strict corrupt-state check and `--reset-state`
(2.4); `parked` and `--unpark` (2.7); `bootstrap.py` taking the lock itself and the 21:10 guard (2.1, 5); the
separate `new-releases-state.json` with `browse_id` and `evaluated` (4.5); the record id form (4.3); no names in
the shipped log (4.7); storing the artist `mbid` (2.4). Statements of #165-5, #171 and #172 that carry the date
watermark model (`since`, `watermark`, `lookback_days`, "newer than the last check") are stale; the planner
edits them, this PR does not.

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
- The release source (section 4.4) was measured live by the owner's probe on 2 artists only (read from the #164
  comments, not repeated here): singles year presence for every artist, first-page size, same-name artists and the
  YouTube Music terms are open. The real request counts per artist per night (4.6) are unmeasured.
- The behaviour of `ArtistTracks` statuses, `RunState.artist_due` and `record_artist` is read from code on master
  and `run_lastfm` on master, not run.
- Real call counts, run times and track counts per artist (everything marked "unmeasured"); the owner's profile
  privacy and the duration quality of Last.fm records are measured by #153/#154 and the first sessions.
- The wording of the Last.fm terms.
- Function names and behaviour of the engine are read from master after the merge of #162; #166 and #172 will
  change them, and this page must be updated with them.
- Whether systemd re-runs a catch-up after a run that exited 75.

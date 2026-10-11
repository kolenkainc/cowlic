# Bootstrap runbook: fill the library from the Last.fm ranking

How to run the one-time bootstrap (umbrella #102, ticket #170). Design and contracts: [bootstrap-ongoing.md](bootstrap-ongoing.md)
(that page is the design, this one is the how-to). Background for the engine, the log and the timer:
[local-ingest.md](local-ingest.md).

Who does what (owner decision 2026-10-11, #165 question 7): the coordinator or an agent starts the sessions from the
main checkout, through the wrapper, as soon as the feature is merged and reviewed. The owner does not start sessions;
the owner reads the log and the status file and can interrupt a session at any time (section 4). The nightly 21:10 run
stays lean and never does a bootstrap (section 8).

Public repo: no secret, no real Last.fm user name and no id on this page. Only the names `LASTFM_API_KEY` and
`COWLIC_EXTRA_SECRET` of variables appear, never a value.

## 0. How to read this page

- **[V]** the command was actually run, read-only and offline, by the author while writing this page. Which ones: the
  `--help` output of `ingest/bootstrap.py` and of each of its four subcommands (`rank`, `plan`, `run`, `status`);
  `ingest/bootstrap.py status` against an EMPTY temporary state directory (it printed
  `no usable ranking: no ranking file; run rank first`, exit 0); the wrapper `scripts/run-bootstrap-local.sh` with no
  arguments and with `run --max-new 5` and no `--max-minutes` (both refused with one usage line, exit 78; usage and cap
  errors and the 21:10 guard touch nothing, other exit-78 cases do, see section 5). Nothing else was run.
- **Not run by the author**: every other command and every behaviour described here. These are read from
  `scripts/run-bootstrap-local.sh`, `ingest/bootstrap.py`, `ingest/bootstrap_ranking.py` and `docs/bootstrap-ongoing.md`
  on master (after PRs #231, #232 and #236). No call to Last.fm, YouTube, D1 or Telegram was made for this page.
- **[R]** reported or an estimate, not confirmed in a primary source.
- The wrapper has no `--help`; its usage is in the header of `scripts/run-bootstrap-local.sh` and in the one-line usage
  message it prints [V].

All commands run from the repository root of the main checkout (the wrapper changes into it itself; `bootstrap.py` reads
`./.env` from the current directory).

## 1. Prerequisites

1. The ingest environment works: `ingest/.venv` exists, `.env` holds the usual ingest variables (Telegram, Cloudflare
   D1, see [local-ingest.md](local-ingest.md)). `run` and `plan` need the D1 variables; `rank` does not.
2. `LASTFM_API_KEY` is set in the local `.env` (name only here, never in a command line, a log or a file in the repo). A
   missing or malformed key is a one-line refusal with exit 2 (`LASTFM_API_KEY is not set (put it in .env)`). Never
   read, print or paste `.env`: the scripts load it themselves.
3. `flock` (util-linux) and the other prerequisites of `scripts/check-local-prereqs.sh`: the wrapper runs that check
   and exits 78 when it fails.
4. The Last.fm profile is public (a private profile is Last.fm error 17, see section 7).
5. The local config file, OUTSIDE the repo. Default location:
   `${XDG_CONFIG_HOME:-$HOME/.config}/cowlic/bootstrap.yml`; any other path is passed with `--config FILE` (every
   subcommand accepts it). Why it is not in the repo: the Last.fm user name must never go into this public repository,
   so the file is a separate local file and is never committed. Do not put the user name into `sources.yml`, a ticket,
   a PR or a log either. If it must never reach the log at all, put it in `COWLIC_EXTRA_SECRET`, which the wrapper's
   log filter redacts (see the header of `scripts/run-bootstrap-local.sh`).

Example (FAKE user name; replace it locally):

```yaml
lastfm_artists:
  user: example_user
  years: 5
  max_artists: 100
  exclude:
    - Various Artists
```

Rules of the file (the strict parser of the `lastfm_artists:` section, shared with `sources.yml`): two-space keys,
four-space list items, no inline comments, no quotes, unknown or repeated keys are errors. The file must hold this
section and nothing else (playlists or `followed_artists:` are refused). `user` (2-15 letters, digits, `_`, `-`) is
read by `rank` only. `years` is 1..10 (default 5), `max_artists` 1..10000 (default 100; the Last.fm client refuses above
1000), `exclude` is a list of artist names. `enabled` and `rescan_days` have no meaning for bootstrap, but the shared
parser still validates the file, so `enabled: false` or a missing `user` is refused as a bad config even for `run`,
`plan` and `status`. Create it with owner-only permissions:

```sh
mkdir -p ~/.config/cowlic
chmod 700 ~/.config/cowlic
# write bootstrap.yml there (an editor, not a shell redirect with a real name in your history), then:
chmod 600 ~/.config/cowlic/bootstrap.yml
```

Files bootstrap creates (names only; no contents are described or shown here) in the state directory
`${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/` (directory 0700, files 0600):

| File | Written by | Purpose |
|---|---|---|
| `bootstrap-ranking.json` | `rank` | The stored ranking: what to work through, in priority order |
| `bootstrap-progress.json` | `run` | The single truth about each artist (`pending`, `partial`, `done`, `parked`) |
| `bootstrap-state.json` | the engine (`run`) | Track cooldowns and scan dates; never the nightly `ingest-state.json` |
| `bootstrap.log` | the wrapper | Output of every wrapper invocation that got past the lock, rotated by size (`.1` .. `.N`) |
| `bootstrap-last-run.json` | the wrapper | Result of the last invocation (section 5) |
| `cover-backfill.json` | `covers` | Cooldown of albums without a match or with a mismatch: album id (a hash), result word, date; no names (section 11) |
| `ingest.lock` | wrapper and nightly | The ONE lock shared with the 21:10 timer |

Bootstrap ships LOGS ONLY to Grafana Cloud Loki (#244), see section 5: no metrics, no Influx push, no
`cowlic_ingest_*` series, so the `cowlic-ingest` dashboard keeps meaning the 21:10 run. Progress is read from `status`,
`bootstrap-last-run.json`, `bootstrap.log` and, live, from Loki. The wrapper never touches `ingest.log` or `last-run.json`.

## 2. Order of steps

1. **The gate comes first.** The Last.fm match-rate report (#153) and the go/no-go (#154) are both closed. The verdict
   of #154 is GO on 2026-10-11: matched 117 of 150 tracks (78.0%; duration mode 76.3%, title-only mode 14 of 15) after
   the matcher change #194, BELOW the 80% the #92 gate asks for, accepted under the owner's own fallback rule "76% is OK to go"; the GO was recorded by the coordinator on the owner's behalf and the owner can reverse it. The wrong-rate half is unmeasured and accepted (section 9). Nothing else is required before
   the first session: the sample labelling of #214 is NOT a blocker (section 9).
2. **Config and key** (section 1).
3. **`rank`**, once: computes the ranking from the weekly charts (about 261 Last.fm calls at 1 call per second, about 5
   minutes [read from the design page and `lastfm.py`]) and stores it. Refuses with one line when a ranking
   already exists unless `--refresh`.
4. **`status`**: confirm there is a ranking and nothing is `partial` yet.
5. **`plan`**, a dry run with the same caps as the session you intend to run.
6. **A small first `run`**, then **repeated sessions** until `status` says `done` (section 4).

Commands (the wrapper is the normal entry; `--config FILE` is optional when the file is in the default place):

```sh
scripts/run-bootstrap-local.sh rank
scripts/run-bootstrap-local.sh status
scripts/run-bootstrap-local.sh plan --max-new 10 --max-minutes 20
scripts/run-bootstrap-local.sh run  --max-new 10 --max-minutes 20
```

(`--config FILE` goes after the subcommand: `scripts/run-bootstrap-local.sh plan --config FILE --max-new 10 --max-minutes 20`.)
Not run by the author. Both `--max-new N` (1..1000000) and `--max-minutes M` (1..1440) are REQUIRED for `plan` and
`run`, each exactly once, `--opt N` or `--opt=N`; `rank` and `status` take no `--max-*`. A missing or out-of-range cap is
exit 78 from the wrapper [V for the missing cap] and exit 2 from `bootstrap.py`. `--result-json` belongs to the wrapper:
do not pass it (or any abbreviation of it).

What `plan` does: a dry run. It reads D1, searches YouTube Music (only searches that produce a match count against `--max-new`; no-match searches do not) and may call
Last.fm to enumerate artists; it downloads, uploads and writes nothing (no state file). It prints one line:
`plan: the next session (--max-new N, --max-minutes M) would reach X of Y artists (rank positions A-B): C complete,
D cut by a limit, E whose enumeration failed; it would attempt T track(s); nothing was downloaded, uploaded or written`.
`plan` is not free, so use the caps you plan to run with, not larger ones.

**The first session is small.** After a clean `plan`, run a session with small caps (the example above: 10 tracks,
20 minutes), read the log and `bootstrap-last-run.json` (section 5), and only then raise the caps. A track costs
about a minute with pacing [R, the figure of the local-ingest runbook; unmeasured for bootstrap, real counts come
from the first sessions].

### Choosing `--max-new` and `--max-minutes`

- `--max-new` counts tracks attempted (new, too big, failed); tracks already stored, in cooldown or without a match
  are not counted. `--max-minutes` is wall clock: no new track starts after it, a download in progress finishes, and
  the Last.fm enumeration time counts too. The limit is checked only before an artist is enumerated and before each track, so the session can overrun
  `--max-minutes` by an enumeration that started before the deadline (up to about 2.5 minutes) plus one track and
  its upload. Leave a margin.
- Both are caps, whichever comes first stops the session. With about a minute per track, `--max-new` of about
  `--max-minutes` is a sensible pair; set the second by what you want to stop on.
- **A session must end before the next 21:10 (local time).** The lock is shared with the nightly timer. The wrapper
  REFUSES to start a `run` or `plan` when `now + --max-minutes + 10 minutes margin + 60 s` (the timer's random delay)
  reaches the next 21:10 local time: one stderr line that says how many minutes fit now, exit 78, nothing touched.
  Choose `--max-minutes` at most that number. Within 60 s after 21:10 that day's 21:10 still counts as the next one.
- **The lock.** If the timer's run (or another session) holds `ingest.lock`, the wrapper exits **75** at once with one
  stderr line and does nothing. Start the session after the 21:10 run finished. The reverse also holds: if a session is
  still running at 21:10, the timer's run exits 75 and does nothing that night. A night lost to the lock leaves NO
  record in `ingest.log`, `last-run.json` or Grafana (the locked invocation touches none of them and ships nothing), and the dashboard
  only flags more than 14 days without a run. The next night continues and nothing is lost but that one night. Whether
  systemd catches up a run that exited 75 is [R], not verified; do not count on it.
- **Things the guard cannot see.** The timer has `Persistent=true`: after a sleep, wake or login the nightly run can
  start at an arbitrary time and collide with a running session (it then exits 75 and that night is lost). A suspend
  during a session makes the wall-clock cap meaningless. `rank` (about 5 minutes under the lock) has no 21:10 guard in
  the wrapper: start it well before 21:10.

## 3. Reading `status`

`status` needs no network, prints no names and no titles, and exits 0 even when it has to report a problem. Through the
wrapper it takes the lock too (so it exits 75 while a session or the timer run is active). To look while a session is
running, call the Python entry point directly, which takes no lock for `status`:

```sh
ingest/.venv/bin/python ingest/bootstrap.py status
```

(Without `--config`, the default config file `${XDG_CONFIG_HOME:-$HOME/.config}/cowlic/bootstrap.yml` is used when it
exists, for `status`, `plan` and `run` alike, and its `exclude` list is left out of N; `--config FILE` overrides it. With
no file at all, nothing is excluded.)

| Output | Meaning |
|---|---|
| `done: N artists, M tracks` | End state: every ranked artist (excluded ones left out) is `done`; M tracks were added by bootstrap |
| `done: N artists, M tracks, K parked` | Done EXCEPT K artists that need a decision (below) |
| `partial: X of N artists done, M tracks, next: <rank position>` (plus `, K parked`) | Work remains; the next artist to work has this rank position |
| second line `empty: E artists with no tracks found (unknown or no albums), truncated: T artists` | Appears when E or T is non-zero. An empty artist is a spelling gap (Last.fm knows the name differently from YouTube Music or has no albums), NOT a success. `truncated`: the artist had more albums than the enumeration limit (100) |
| `no usable ranking: <reason>; run rank first` | No ranking stored, or the file is unusable [V for the empty-directory case] |
| `state file unusable: <file>: <reason>; run --reset-state moves it aside and starts empty` | A progress or state file is corrupt, too big or of an unknown version (section 7) |
| `config unusable: <reason>` | The `--config` file is bad |

`M` counts tracks bootstrap added, not tracks the library already held from playlists. A session killed inside an artist
loses that artist's count for that session (the tracks are in D1 and skipped next time), so M can undercount.

`done` for an artist means "enumerated to the end and each track handled once", NOT "every track is stored":
`no_match`, `too_big` (mp3 over 20 MB, about 10 minutes at 256k) and cooldown failures are part of a `done` artist, and
they are not retried by bootstrap.

**Parked artists.** An artist whose enumeration failed 3 sessions in a row becomes `parked`: skipped by `run` and `plan`,
never retried automatically. Decide: `run --unpark` makes every parked artist `pending` again (failures reset), or add
the artist to `exclude` in the config. `exclude` only REMOVES an artist from the work list; it cannot fix a spelling.

## 4. A session, resuming, interrupting

A session is one `run`. Work goes artist by artist in priority order (5-year playcount descending); a session cut by the
cap, the time limit or a sign-in stop leaves the current artist `partial`, and the next `run` resumes it first.
Resuming costs the artist's Last.fm enumeration again (up to about 150 requests, about 2.5 minutes [R, arithmetic]);
tracks already stored are skipped, so only what is missing is added. There is no separate "resume" command: run
the same command again.

Stop reasons (`stop_reason` in `bootstrap-last-run.json` and the `stop reason:` line of the log). The exit code is 1
instead of 0 when a track or artist failed during the session, whatever the stop reason:

| Stop reason | Exit | Meaning and what to do |
|---|---|---|
| `done` | 0 or 1 | Nothing left to work (also the "nothing to do" case). Read `status` |
| `cap` | 0 or 1 | `--max-new` reached. Normal. Run the next session |
| `time` | 0 or 1 | `--max-minutes` reached. Normal. Run the next session |
| `sign-in` | 3 | YouTube asked to sign in (the bot check, #62). **Wait and run again later; if a second session in a row ends with exit 3, stop and report to the owner, never loop. Never bypass:** no cookies, no proxies, no PO-token tricks, no client rotation, nothing stored. Do not retry in a loop; one stop ends the whole session |
| (empty) | 1 or 2 | The session failed before it ran (for example D1 or Last.fm unreachable), or stopped on an engine error part-way (for example a D1 error; part of the work may have run), or exit 2/75/78 before it started; read the log or the message |

Other exit codes of the wrapper: **2** bad usage, bad config, missing environment, no usable ranking or an unusable
state file (nothing ran; one line says which); **75** locked; **78** usage, guard (would cross 21:10), missing venv or
prerequisites (nothing ran); **128+N** interrupted by signal N.

**Interrupting safely.** Stop a session with Ctrl-C in the terminal or `kill -TERM <wrapper pid>`. The wrapper stops the
whole process group (the session and its helpers) and ends with 128+N; it still writes `bootstrap-last-run.json`. State
is written atomically after each change, so at most the current track is lost. A kill between the Telegram upload and
the D1 write can leave one orphan message in the channel, and that track is uploaded again next session [R, not tested].
Do not use `kill -9` unless the session does not stop: after it a watchdog ends the process group with TERM within about
a second, KILL only after up to 10 more seconds, and only then releases the lock (so at most about 11 seconds), so no session keeps running without the lock. Interrupting never needs a cleanup
step; start the next session when you want to continue.

## 5. Reading `bootstrap-last-run.json` and `bootstrap.log`

Both are in the state directory. The log holds the output of every invocation that got past the lock and the guard
(secrets redacted by `ingest/ship_grafana.py --filter`). The engine writes artist names and track titles into it
(`artist <name>: ...`, `ingested: <artist> - <title>`), so the file is local and mode 0600; do not paste it into a
ticket. What bootstrap itself writes (status, summaries, the progress and result files) holds no names and no titles.

### Grafana Loki (logs only)

`run`, `plan` and `covers` (a real session, not `covers --count`) also ship the session log to your Grafana Cloud Loki, live,
under the label `job="cowlic-bootstrap"` (`rank`, `status` and `covers --count` ship nothing; decision of #250: a covers
session is shipped exactly like a run, the covers lines hold counts and class words only). Nothing else is sent: no `run_summary` line, no Influx push, no `cowlic_ingest_*`
series, nothing from `bootstrap-last-run.json`.

- **What is shipped (decision of #244).** The whole session log from the wrapper's `start:` line to its `finished:` line,
  exactly as the nightly ships `ingest.log` (the nightly log carries the same engine lines for playlists, with names):
  the shared engine's lines with artist names and track titles (`artist <name>: ...`, `ingested: <artist> - <title>`,
  `dry-run would ingest: ...`, `ERROR <artist> - <title>: ...`), the wrapper's own lines and bootstrap's summaries. No
  stricter and no weaker filter than the nightly's. Secrets are redacted before the line reaches the log and again by
  the shipper: `LASTFM_API_KEY`, the Grafana token, and `COWLIC_EXTRA_SECRET` (put the Last.fm user name there). Your
  listening data goes to your own Grafana Cloud only. This differs from the earlier design for the ongoing phase
  (`bootstrap-ongoing.md` 4.7: counts and hashed keys only in what the NIGHTLY ships): that rule stays for the nightly
  new-release phase; bootstrap is a session you start and watch, and the owner asked for the readable log.
- **Keys** (names only, in `.env`, read by the shipper): `INGEST_GRAFANA_LOKI_URL`, `INGEST_GRAFANA_LOKI_USER`,
  `INGEST_GRAFANA_TOKEN`; no new secret. With any of them missing or empty the shipper logs `grafana: not configured`
  once at the end and sends nothing.
- **Live.** Every 20 seconds (`COWLIC_BOOTSTRAP_SHIP_INTERVAL`) the new complete lines are pushed (at most 256 KiB per
  push), and once more at the end including an unfinished last line. Best effort: 30 s limit per push
  (`COWLIC_SHIP_TIMEOUT`), no retry loop, an unreachable Loki or a bad token never changes the exit code, the lock, the
  guard or the signal handling of the wrapper. The last status line (`grafana: loki push ok (HTTP 204)`, or the failure
  class) is appended to `bootstrap.log` after the `finished:` line. A locked (75), refused (78) or usage-failed
  invocation ships nothing.
- **Watch a session.** Grafana, Explore, the Loki data source of your stack, time range the last 15 minutes while it runs:

```logql
{job="cowlic-bootstrap"}                                      # the whole session, newest last
{job="cowlic-bootstrap", level=~"warn|error"}                 # problems only
{job="cowlic-bootstrap"} |= "finished:"                       # one line per finished invocation, with its exit code
```

  A new session starts with the line `run-bootstrap-local: start: run ...`. The `level` label is set by the shipper from a
  leading `ERROR` or `WARN` token (the engine's lines); the wrapper's own `run-bootstrap-local: ERROR ...` lines stay
  `info` (the shipper only knows the nightly prefix), so search them with `|= "ERROR"`.
- **Dashboard.** `deploy/grafana/cowlic-ingest-dashboard.json` is unchanged (its `Last.fm report (live, logs only)`
  panel is the model). Proposal, not done here: one additive logs panel `Bootstrap session (live, logs only)` with the
  query `{job="cowlic-bootstrap"}` next to that panel; it is optional, Explore is enough for a session you start
  yourself.

`bootstrap-last-run.json` is removed and rewritten by EVERY wrapper invocation that gets past the lock and the guard,
including `status`, `rank` and `plan`: a wrapper `status` after a `run` replaces the session's result with
`subcommand: status` and `result: null`. **Read the file (and copy what you need) before any other wrapper call**, or
check with the direct `ingest/.venv/bin/python ingest/bootstrap.py status`. A killed wrapper leaves none. Usage and cap
errors, the 21:10 guard and a held lock (75) touch nothing; a missing venv or a failed prerequisite check (also exit 78)
happens after the file was removed and the log opened, so it rewrites the file with `exit_code` 78. Always
valid JSON, written atomically. Fields: `started_at`, `finished_at`, `subcommand`, `exit_code`, `stop_reason`
(`done`, `cap`, `time`, `sign-in`, or empty), `log_file`, and `result`: for `run` the object of `--result-json` (counts
only), otherwise `null`. The `result` object holds `stop_reason`, `exit_code`, `counts` (`added`, `skipped`,
`no_match`, `too_big`, `failed`, `matched_title_only`, `cooldown`, `not_attempted`) and `artists` (`planned`,
`completed`, `cut`, `failed`, `not_started`, `parked`, `unusable`).

In the log a session prints: `bootstrap session N: P artist(s) to work, --max-new ..., --max-minutes ...`, one line
per artist and per track from the engine, a summary line, `bootstrap session N: X artist(s) completed, Y cut, Z
enumeration(s) failed, W newly parked`, `stop reason: ...`, and the `status` lines. `matched by title only` marks the
weaker title-only matches (no duration check); they are counted in `matched_title_only`.

## 6. The end state and what comes after

The end state is the `status` line `done: N artists, M tracks` (with `, K parked` when parked artists remain and the
`empty:` line when some artists had nothing to enumerate). When it appears:

1. Read the second line and the parked count; decide parked artists (`--unpark` or `exclude`) and accept the empty
   artists as a known gap (a spelling gap is not fixed by `exclude`).
2. No more sessions are needed. The nightly 21:10 run takes over: it stays lean (unit caps, no ranking, no backfill,
   section 8). The nightly new-release check (#172, merged) runs EVERY night after the playlists, bounded by its own
   caps, and reads only the stored ranking file; nobody starts it by hand. It is OFF in the committed `sources.yml`
   (no `new_releases:` section) and stays off until #238 (a partly landed release is not retried) is fixed.
3. Do NOT run `rank --refresh` as part of "finishing"; it replaces the ranking and makes new artists `pending`. Do it
   only on purpose, with the same steps again (`status`, `plan`, a small `run`).

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `LASTFM_API_KEY is not set (put it in .env)` or `... does not look like a Last.fm API key` | Missing or malformed key (exit 2). Put the key in the local `.env`; do not paste it anywhere. `invalid API key (error 10)` means Last.fm refused it |
| `no config file: pass --config FILE ...` (from `rank`) or `bad config: ...` | `rank` needs a config with `user`; check the file against section 1 (strict format, only the `lastfm_artists:` section). The message never contains the user name |
| Private or empty Last.fm profile: `login required (error 17): the profile or its listening data is private; make it public`, `no weekly charts in the window ...`, or `the weekly charts hold no artists ...` | Make the profile and its listening data public, or check the user name (`user not found (error 6)`) and `years`. `rank` exits 2 for these and writes no ranking |
| `no usable ranking (<reason>): run rank first` (exit 2 from `run` and `plan`; `status` prints `no usable ranking: ...`) | No ranking file, or it is corrupt or of an unknown version. Run `rank`. `run` never computes the ranking itself and never recomputes silently |
| `a ranking already exists; pass refresh=True (--refresh) to replace it` | `rank` was run before. Use `rank --refresh` only on purpose; the old file stays when the new compute fails |
| Sign-in stop: exit 3, log line about the YouTube bot check | Wait and run again later. Never add cookies, proxies, PO tokens or client rotation (#62) |
| `another run or session is active (lock .../ingest.lock); exiting without doing anything`, exit 75 | The 21:10 run or another session holds the lock. Wait until it ends and start again. No record is left by the locked invocation |
| `refusing to start: now + --max-minutes M + 10 min margin reaches the next 21:10 local time (at most F minutes fit now)`, exit 78 | Lower `--max-minutes` to at most F, or start after the 21:10 run |
| Partial artist (`status`: `partial: ... next: <rank>`) | Normal after a cap, time or sign-in stop. Run the next session; the partial artist is resumed first |
| Parked artists (`, K parked`) | 3 failed enumerations in a row. `run --unpark` or add them to `exclude` (section 3) |
| `state file unusable: <file>: <reason>` (`status`, exit 0) or `bootstrap: state file unusable: ...` (`run` and `plan`, exit 2) | A progress or state file is corrupt, too big, of an unknown version, or an entry would be dropped. `run --reset-state` MOVES both `bootstrap-progress.json` and `bootstrap-state.json` to a numbered `.bak` (`.bak`, `.bak.1`, ...; never deletes, never overwrites) and starts empty. `--reset-state` is an option of `run`: `run --reset-state --max-new N --max-minutes M` moves the files and then STARTS a real session with those caps in the same call. Effects: artists restart from the first and finished artists are re-enumerated (about 2.5 minutes each); tracks already stored are skipped, but the track cooldowns are gone too, so earlier `no_match`, `too_big` and failed tracks are attempted again and count against `--max-new`; `status` M restarts at 0. Use a small cap after a reset. The ranking and the nightly state are not touched |
| `config unusable: ...` from `status` | The `--config` file is bad; fix it. `status` still exits 0 |
| The wrapper exits 78 with a prerequisites message | `scripts/check-local-prereqs.sh` failed (flock, venv, ffmpeg, python; a Deno problem is only a WARN line); the log lists what is missing. Nothing ran |
| `run` says `nothing to do: every ranked artist is done, parked or excluded` | Read `status`; `--unpark` if artists are parked |

## 8. The nightly run must not carry the ranking

The nightly `sources.yml` must NOT contain a `lastfm_artists:` key, not even with `enabled: false`. The nightly wrapper
refuses it (whole-line grep, exit 78, before ingest starts; #174), so a bootstrap config never belongs in
`sources.yml`: it is a separate local file outside the repo, passed with `--config`. The 21:10 unit sets the caps
`--max-new 30 --max-minutes 30` and `TimeoutStartSec=1h`; the installed copy changes only when the owner copies the
unit (#175). The old section of `docs/local-ingest.md` that describes enabling `lastfm_artists:` carries a note about
this; the first capped run through `sources.yml` of #155 is superseded by this runbook (#176).

## 9. Accepted risk: the wrong-recording rate is not measured

The gate of assessment #92 has two halves: matched rate at least 80% and wrong rate at most 2%. The matched half was
measured at 78.0% (117 of 150), BELOW 80%; #154 records the GO as a decision by the coordinator on the owner's behalf,
under the owner's own rule "76% is ok to go", reversible by the owner (section 2). The wrong rate, "the matched video is the wrong recording", cannot be computed by the code: it
needs a human to label a sample of the matched rows `ok` or `wrong` in the `reviewed` column of the report CSV, and
the owner decided on 2026-10-11 (#214, closed as not planned) that this labelling will NOT happen ("I will not review
anything by hand, the match quality from the code is acceptable to me"). So:

- The wrong-recording rate is an **accepted, unmeasured risk**. Some tracks in the library may be a wrong recording
  (a live or cover version, or another artist's track with the same title). Nobody knows how many.
- Mitigations in the code and the process:
  1. The 3-second duration check: when Last.fm gives a duration, a match needs the YouTube duration within 3 s.
  2. The title-only mode (no Last.fm duration) is stricter on titles and artists, and is logged
     (`matched by title only`) and counted separately (`matched_title_only` in the result), so it can be looked at.
  3. Sessions are capped and the first one is small, after a `plan` dry run (section 2), so a systematic problem shows
     up in a small batch before it grows.
- This is reversible. The labelling tooling still exists (`ingest/lastfm_report.py --summarize`, see
  [local-ingest.md](local-ingest.md)); if the owner changes the decision, labelling a sample and running it needs no
  change to bootstrap, and a no-go at that point is a new decision that stops the sessions.
- Labelling is optional tooling, not a gate: its absence blocks nothing in this runbook.

## 10. Limits and terms

- **Last.fm use:** non-commercial, personal use; names, playcounts, album and track names only; no artwork and no audio
  from Last.fm (no Last.fm artwork is used: covers of Last.fm tracks come from YouTube Music, section 11); at most 1 call per second with bounded backoff. The clause
  numbers (3.1, 5.1.8) are [R]: re-check them against https://www.last.fm/api/tos before relying on one.
- **The stored ranking is a derived list** (artist names, playcounts, the computation date; no user name). The owner
  accepted storing it locally once, recomputed only with `--refresh` (#165 question 3, answered on 2026-10-11, "default
  options are ok"). The wording of the Last.fm caching clause itself is still [R], not independently confirmed.
- **YouTube:** the bot check is never bypassed (#62). Pacing is the engine default (5 s plus 0..5 s between downloads).
- **Last.fm songs are downloaded by yt-dlp directly (#240).** They have no Spotify URL, which spotdl's download needs,
  so the engine skips spotdl for them: yt-dlp fetches the matched video as mp3 at 256k (no cookies, no config file) and
  the engine writes the ID3 tags itself (title, artists, album, album artist, track, disc). Playlist and followed-artist
  songs still go through spotdl, unchanged. Sign-in stop, `too_big` and upload behave the same for both. The first real
  bootstrap session of #176 is the verification of this path on real Last.fm songs.
- **Telegram:** `getFile` limit 20 MB, so a track over about 10 minutes at 256k is `too_big` and skipped.
- **Not run by the author:** every step of sections 2 to 7 except the offline commands listed in section 0. Real call
  counts, run times and track counts per artist are unmeasured; the first sessions give them.

## 11. Backfill the covers of albums stored without one (`covers`, #250)

Albums that were stored before album covers existed (#248) have `cover_file_id` empty. `covers` gives them one WITHOUT
downloading audio. Design: [cover-art.md](cover-art.md) section 7. Artist photos are NOT part of it (#253).

**When.** BETWEEN sessions, never during a bootstrap session: it takes the same lock as `run` and the 21:10 timer, so
while one of them runs it exits 75 and does nothing (the reverse holds too: a covers session still running at 21:10 makes
that night's timer run exit 75). It does not read or write the Last.fm ranking, the bootstrap progress file or any engine
state; it needs no Last.fm key and no config file, only the Telegram and Cloudflare D1 variables of the ingest `.env`
(`--count` needs only the D1 ones).

**What one session does.** It reads from D1 the albums with `cover_file_id IS NULL` (an album named like the placeholder
`Unknown Album` is never filled; an album without a track is not selected), in id order, one stored track per album, and for
each one:

1. runs the SAME matcher as ingest on that track (1 to 3 YouTube Music searches, paced and retried, below);
2. applies the cover guard of #248: the matched result's album name must be the stored album (an edition word such as
   deluxe or live, another volume or number, or a bare plural `s` is another release) and its artist must be the stored
   artist (the Latin/Cyrillic-aware check); otherwise the album is skipped and counted, and stays empty;
3. fetches the thumbnail through the hardened route of #248 (https, host allowlist, no redirects, `image/jpeg`, JPEG magic
   bytes, 10 MB and a total time limit) and sends it as a Telegram photo (the same `sendPhoto` path of ingest);
4. writes `UPDATE albums SET cover_file_id = ? WHERE id = ? AND cover_file_id IS NULL` (parameterized). A cover another
   process stored meanwhile is never replaced (counted `already`). An orphan Telegram photo is left when that happens and also
   when the upload worked but the D1 `UPDATE` then failed (the album counts `failed`; the next try uploads another photo).

Nothing is downloaded as audio and no track row is touched.

### Commands

```sh
scripts/run-bootstrap-local.sh covers --count
scripts/run-bootstrap-local.sh covers --max-new 3 --max-minutes 5
scripts/run-bootstrap-local.sh covers --max-new 50 --max-minutes 30
```

Not run by the author (section 0). Order: `--count` first, then a small session, read the log and
`bootstrap-last-run.json`, then raise the caps; repeat until the count says nothing is left to try.

- **`covers --count`** is read-only: D1 reads only, no YouTube call, no Telegram call, no write, no file. It takes no
  `--max-new` and no `--max-minutes` (the wrapper rejects them with `--count`, exit 78), has no 21:10 guard (it ends in
  seconds) and still takes the lock (exit 75 while a session or the timer run holds it). It prints counts only:
  `covers: albums without a cover: N (to try: T, in cooldown: C, unknown album: U, unreadable rows: B; retry cooldown D days)`
  and `covers: artists without an image: ...`. The artist half is `not counted yet (the artists.image_file_id column does not
  exist)` until the artist-image migration is in production (#249); the output shape already has the line.
- **`covers --max-new N --max-minutes M`** needs BOTH caps (1..1000000 and 1..1440, the wrapper and `bootstrap.py` both
  refuse a missing or out-of-range one). `--max-new` counts albums ATTEMPTED (every attempt costs search calls, matched or
  not); albums resting in the cooldown are skipped and not counted. `--max-minutes` is wall clock: no new album starts
  after it, one album in progress finishes (up to 3 paced searches plus retries; leave a margin of a few minutes).
- **`--retry-days D`** (0..3650, default 30; also accepted by `--count`): how long an album without a match (`no_match`) or
  with a mismatch rests before it is tried again. `0` retries them every session.
- **`--retry-failed-days D`** (0..3650, default 3; also accepted by `--count`): the same for an album whose attempt FAILED
  (thumbnail 404, not a JPEG, too large, Telegram or D1 refusing, any unexpected error). Without it an album that fails the
  same way every time would stay first in line and three of them would end every session. `0` retries them every session.
- **The 21:10 guard** is the one of `run`: the wrapper (and `bootstrap.py` itself) refuse to start when `now + --max-minutes
  + 10 min + 60 s` reaches the next 21:10 local time (exit 78, one line, nothing touched). `--count` is exempt.
- **Pacing and retries.** At least 1.5 s plus 0..1 s of jitter between two YouTube Music calls; a transient error (HTTP
  429/5xx, network) is retried at most twice with a growing wait; any other error is not retried. Three failed albums in a
  row stop the session (stop reason `failed`, exit 1: D1, Telegram or the network are probably down).
- **Sign-in stop.** A YouTube bot check ends the session at once with exit 3 (stop reason `sign-in`), is never retried
  and never bypassed: no cookies, no proxies, no PO-token tricks, no client rotation (#62). Wait and run again later; if
  a second session in a row ends with exit 3, stop and report to the owner.

### Resuming, the cooldown file, results

The work list comes from D1 every time, so a stopped or killed session resumes by itself: albums that got a cover are no
longer selected. `cover-backfill.json` in the state directory (0600, atomic writes, directory 0700) remembers only the
hopeless ones: `{"version": 1, "albums": {<album id>: {"result": "no_match" | "mismatch", "on": "YYYY-MM-DD"}}}` (ids are
hashes, no names; `result` may also be `failed`). A later success removes the entry. A failed album rests for
`--retry-failed-days`, so a session moves past it; a short outage can therefore rest up to three albums for three days. A file that is not valid (unknown field, bad date, wrong
version, not a regular file) is refused with exit 2 and nothing runs; to start empty, move it aside by hand (there is no
`--reset-state` for `covers`).

Log lines are counts and class words only, never a name or a title (`covers: album 7: no_match`, a summary
`covers: covered 12, already 0, no_match 3, album_mismatch 5, artist_mismatch 1, no_cover_in_match 0, failed 0, cooldown 4,
not attempted 20`, `covers: R album(s) still without a cover`, `stop reason: cap | time | done | sign-in | failed`).
Outcome words: `covered`, `already` (filled meanwhile), `no_match` (the matcher found nothing), `album_mismatch`,
`artist_mismatch` (the guard refused the match), `no_cover_in_match` (the match has no usable thumbnail or album),
`failed`. `bootstrap-last-run.json` holds `subcommand: covers` and, for a session, the result object (`stop_reason`,
`exit_code`, `counts` with the words above plus `cooldown` and `not_attempted`; `artists` is empty). Exit codes: 0 ok (a cap,
time or empty-list stop is not a failure), 1 a failed album or an unsaved cooldown file, 2 usage, missing environment or an
unusable cooldown file, 3 sign-in stop, 75 locked, 78 guard or wrapper usage.

### Limits of this runbook

- The similarity thresholds of the guard were tested offline with invented names, not on the real library; the first
  sessions show how many albums end `album_mismatch` or `no_match`. A high count is a reason to look at the guard, not to
  loosen it blindly (a wrong cover stays for good: the first stored cover is never replaced).
- A match can fail when YouTube Music lists the album under another title; such albums rest in the cooldown and stay without a
  cover. Whether to add another source (Cover Art Archive, design doc section 8 item 5) is decided from the first counts.
- Only the stored track with the smallest id represents an album. If it is a live or bonus track the guard refuses the
  match, and the whole album rests for `--retry-days` (30 days) although another of its tracks would have matched. Known
  limit, not worked around.
- The artist guard is one-way for scripts: a stored Cyrillic artist name against a Latin YouTube Music spelling is refused
  (a safe false reject, the same code as #248); the Latin-stored, Cyrillic-listed direction is accepted.
- Search call limits of YouTube Music are undocumented [R]; the pacing above is a conservative guess, not a measured limit.
- Not run by the author: every command of this section. The tests (`ingest/test_bootstrap_covers.py`,
  `scripts/test-run-bootstrap-local.sh`) use fakes and an in-memory SQLite copy of the real schema; no D1, Telegram,
  YouTube or Loki call was made.

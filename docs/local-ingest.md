# Local ingest runbook

How to run and operate the ingest on the owner's laptop. Design background: [architecture.md](architecture.md).

Status of the commands below: the wrapper (including `last-run.json`), the prerequisite check and the units are
merged. Commands marked "read-only checked" were run while writing this page. Everything else (a real run,
enabling the timer) is untested by the author and is done by the owner in #73 and #74.

## What runs where

- Machine: the owner's laptop (decision #72). GitHub Actions does not run ingest: YouTube blocks its runner IPs.
- `scripts/run-ingest-local.sh` is the one command. It takes a lock, rotates the log, runs the prerequisite check,
  then runs `ingest/ingest.py --sources sources.yml`, and writes `last-run.json`.
- `ingest/ingest.py` reads `.env` itself (the wrapper does not), matches tracks on YouTube Music, downloads,
  uploads to the Telegram channel and writes metadata to D1. Tracks already in D1 are skipped, so re-runs add
  no duplicates.
- A systemd user timer starts the wrapper daily at 21:10 machine LOCAL time (`deploy/systemd/`).
- Observability is logs only, no notifications (decision #72): you read the log and `last-run.json`. Optionally
  the redacted log of each run is also pushed to your Grafana Cloud Loki (see
  [Shipping the run log to Grafana Cloud](#shipping-the-run-log-to-grafana-cloud)); without the three keys
  listed there nothing is sent anywhere.

## Prerequisites

Run the read-only check (it installs nothing and prints no `.env` values):

```sh
scripts/check-local-prereqs.sh
```

It prints `ok` or `MISSING` with a fix hint per item and exits 1 if anything is missing.

- `ffmpeg` on PATH.
- Python 3.10+ and the venv: `python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt`
  (Debian/Ubuntu: `sudo apt install python3-venv` first). The check also imports `spotdl` and `ytmusicapi`.
- `flock` (util-linux) and bash 4+.
- `deno` 2.3.0+ is optional. yt-dlp uses it to solve YouTube JavaScript challenges. Without it ingest still runs
  and only logs a warning. The wrapper runs the check with `--deno-optional`, so a missing or old deno is a
  `WARN` line in the log, not a failure. Run `scripts/check-local-prereqs.sh --deno-optional` to see the same
  behaviour by hand. Install it if downloads fail with challenge errors.
- `HOME` must be set (the wrapper puts state under `$HOME`). systemd sets it for user units.
- `.env` at the repo root (gitignored), with these keys (names only; values never go in docs, chat or git):
  `TG_BOT_TOKEN`, `TG_CHANNEL_ID`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`,
  `INGEST_CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`.
  The check only greps key names: it can say `ok` while the first run still fails on syntax. The parser
  (`parse_env` in `ingest/tg_check.py`) accepts blank lines, comment lines (first non-blank char `#`, any text
  after it) and `KEY=value` where KEY is `[A-Za-z_][A-Za-z0-9_]*`. The value is taken verbatim; trailing
  spaces and tabs are trimmed; matching quotes around the whole value are removed; an empty value counts as
  missing. It rejects, with exit 1 (class `config`): `export KEY=...`; spaces around `=`; a value starting with
  whitespace; an unquoted inline comment (` #` in the value); an unbalanced quote or text after the closing
  quote; a repeated key; non-ASCII bytes outside comment lines; a NUL byte; a bare CR (CRLF line ends are
  fine). A syntax error is logged with the line number (and key name) only, never the value.

## Secrets

Secrets live only in the machine's gitignored `.env`. `INGEST_CLOUDFLARE_API_TOKEN` is the narrow token (D1:Edit
only) and is the only Cloudflare token ingest uses; ingest never reads the broad `CLOUDFLARE_API_TOKEN`.
Nothing about ingest is stored in GitHub (the old `ingest` environment secrets are removed in #75). The repo is
public: never paste values into issues, PRs or logs.

## Run once by hand

```sh
scripts/check-local-prereqs.sh
scripts/run-ingest-local.sh
echo $?          # see the exit code table below
cat "${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/last-run.json"
```

The wrapper prints everything it logs to the terminal as well as the log file. To match without downloading or
uploading anything, call ingest directly:

```sh
ingest/.venv/bin/python ingest/ingest.py --sources sources.yml --dry-run
```

`--dry-run` matches only; it still needs `.env` and still reads D1 and the playlists.

## Install the systemd user timer

Assumption: the laptop uses systemd. Check with `systemctl --user status` (it should show a state, not an error).

Units are in `deploy/systemd/`: `cowlic-ingest.service` (one run) and `cowlic-ingest.timer`
(`OnCalendar=*-*-* 21:10:00`, no time zone suffix, so the machine's local time with DST handled by systemd;
`Persistent=true`; `RandomizedDelaySec=60`).

1. Copy the units:

   ```sh
   mkdir -p ~/.config/systemd/user
   cp deploy/systemd/cowlic-ingest.service deploy/systemd/cowlic-ingest.timer ~/.config/systemd/user/
   ```

2. The service hard-codes the repo path `%h/projects/personal/kolenkainc/cowlic` in `WorkingDirectory` and
   `ExecStart`. If your clone is elsewhere, edit both lines in the copied
   `~/.config/systemd/user/cowlic-ingest.service` (keep them identical). `%h` is your home directory. The unit
   also sets `PATH` to include `%h/.deno/bin`; add the directory of `ffmpeg` if it is not in `/usr/bin`.
3. Verify (read-only checked on the repo copies of the units; on this author's machine it printed only an
   unrelated warning about another package's unit):

   ```sh
   systemd-analyze --user verify ~/.config/systemd/user/cowlic-ingest.service ~/.config/systemd/user/cowlic-ingest.timer
   systemd-analyze calendar '*-*-* 21:10:00'      # shows the next elapse in local time
   ```

4. Load and enable (these change state; the owner does this in #74):

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now cowlic-ingest.timer
   systemctl --user list-timers cowlic-ingest.timer
   ```

   `list-timers` shows NEXT (21:10 local) and LAST.

5. Disable and uninstall:

   ```sh
   systemctl --user disable --now cowlic-ingest.timer
   rm ~/.config/systemd/user/cowlic-ingest.service ~/.config/systemd/user/cowlic-ingest.timer
   systemctl --user daemon-reload
   ```

To start one run now through systemd (to test the unit): `systemctl --user start cowlic-ingest.service`.

### Machine off, asleep or logged out

- `Persistent=true`: if the machine was off or asleep at 21:10, systemd starts one catch-up run when the timer
  is active again (after boot or login; on wake it may be after a short delay). No alert is sent; only
  `last-run.json` shows when the last run happened.
- Linger (open question for the owner, to be decided in #74): a user timer only runs while the user's systemd
  instance is running, normally while you are logged in. `loginctl enable-linger $USER` keeps it running
  after logout, so the 21:10 run happens even when you are logged out (the machine must still be on). Trade-off:
  without linger nothing runs while logged out and the run is caught up at next login; with linger the user
  manager stays up permanently. It is only needed if runs are wanted while logged out.

### Optional hardening and the 2 hour limit

- `TimeoutStartSec=2h` stops a hung run. A very large first backlog (many new tracks, each about a minute) may
  need longer: run the first import by hand with `scripts/run-ingest-local.sh` (no time limit), or raise the
  value in the copied unit, then `systemctl --user daemon-reload`. A run killed by the timeout writes no
  `last-run.json` (the previous file stays); look at the log.
- `NoNewPrivileges=yes` can be added to the `[Service]` section of the copied unit. It is optional and untested
  here: the wrapper and ingest need no privilege escalation, but run `systemctl --user start cowlic-ingest.service`
  once after adding it.

## Cron alternative

Use it only if the machine is not systemd-based. Cron has no catch-up: a run missed while the machine was off is
lost until the next day. The overlap lock is inside the wrapper (flock), so the crontab line needs no `flock`.
`crontab -e`:

```cron
10 21 * * * /home/USER/projects/personal/kolenkainc/cowlic/scripts/run-ingest-local.sh >/dev/null 2>&1
```

Replace the path with your clone. Cron uses local time. Cron's environment is minimal: make sure `ffmpeg` is on
the PATH (add `PATH=/usr/local/bin:/usr/bin:/bin:/home/USER/.deno/bin` as a line above; cron does not expand `$HOME` or `~` in
variable assignments, so write the absolute path, see `man 5 crontab`), `HOME` is set by cron. The
wrapper writes its own log, so discarding cron's output is fine. Do not enable both the timer and cron.

## Logs, status and overlap

State directory: `${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/` (mode 700). It holds:

- `ingest.log`: everything the wrapper and ingest print, with their timestamps. Secrets are redacted by ingest.
  Rotated before a run when the file is at least 5 MiB (`ingest.log.1` .. `ingest.log.5` kept; the limits can be
  changed with `COWLIC_LOG_MAX_BYTES` and `COWLIC_LOG_KEEP`).
- `last-run.json`: the status of the latest run. Written after every run that got past the lock.
- `ingest.lock`: the lock file.

Read them:

```sh
tail -n 50 "${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/ingest.log"
cat "${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/last-run.json"
journalctl --user -u cowlic-ingest.service -n 100     # only for runs started by systemd
systemctl --user status cowlic-ingest.service
```

The journal holds the same output as the log for systemd runs (stdout/stderr go to both).

### `last-run.json`

```json
{"started_at": "2026-10-10T19:10:01Z", "finished_at": "2026-10-10T19:12:40Z",
 "exit_code": 0, "failure_class": "none", "log_file": "ingest.log",
 "result": {"playlists": [{"url": "https://open.spotify.com/playlist/<id>", "status": "done",
            "added": 2, "skipped": 1, "no_match": 0, "too_big": 0, "failed": 0, "not_attempted": 0}],
            "counts": {"added": 2, "skipped": 1, "no_match": 0, "too_big": 0, "failed": 0, "not_attempted": 0,
                       "playlists_unreadable": 0, "playlists_not_attempted": 0}}}
```

- `started_at`, `finished_at`: UTC, ISO 8601. `log_file`: file name only.
- `failure_class`: `none` (exit 0), `partial` (some track or playlist failed, exit 1), `config` (bad sources
  or `.env` in ingest, or a missing prerequisite or venv in the wrapper, exit 78), `youtube-sign-in` (exit 3),
  `crash` (non-zero exit with no result from ingest: traceback, kill).
- `result` is ingest's own summary, or `null` when ingest did not run or crashed. Playlist `status`: `done`,
  `unreadable` (could not be fetched), `interrupted` (YouTube sign-in challenge; `not_attempted` counts the
  rest), `not_attempted`. `skipped` means already in the library.
- The file is always valid JSON and holds only counts, class names, timestamps and playlist URLs without query.
- If the wrapper itself is killed (SIGKILL, or systemd stopping the unit at the timeout) no new file is written
  and the previous one stays: compare `finished_at` with the date.

### Overlap

The wrapper takes `flock -n` on `ingest.lock`. If a run is active, a second invocation prints one line to stderr,
exits 75 and does not touch the log or `last-run.json`. This is normal if you start a manual run while the timer
run is still going: wait for it to finish.

## Shipping the run log to Grafana Cloud

After every run (after `last-run.json` is written) the wrapper calls `ingest/ship_grafana.py` (Python standard
library only). It is best effort and cannot change the run: it runs under `timeout` (30 s hard limit), its result
is ignored, the wrapper exit code and `last-run.json` are unchanged, and a failure leaves one short line in
`ingest.log` (`grafana: loki push failed (HTTP 401)`, `... (URLError)`, `grafana shipping timed out`). There is
no retry: the next run is the retry, a failed night is a gap in Grafana.

Enable it with three keys in the machine's `.env` (names only; values come from your Grafana Cloud portal, see
#107, and are never committed or pasted anywhere):

- `INGEST_GRAFANA_LOKI_URL`: the Loki URL of your stack (`https://logs-....grafana.net`; the path
  `/loki/api/v1/push` is added if missing). Must be https and a host ending in `.grafana.net`, otherwise nothing
  is sent.
- `INGEST_GRAFANA_LOKI_USER`: the Loki user (instance id).
- `INGEST_GRAFANA_TOKEN`: an access policy token with the `logs:write` scope only.

With any of them missing the step logs `grafana: not configured` and does nothing.

What is sent, and only that:

- The lines of THIS run: the part of `ingest.log` from the byte offset the wrapper recorded just before its
  `start` line (log rotation happens only there, under the lock, so the offset holds for the whole run). If the
  file is shorter than the offset or the offset is not at a line start, no log lines are sent (this also happens
  on the run after one that was killed mid-line, because the log then has no trailing newline: that run ships
  only the summary). Lines are the ones ingest already redacted; the shipper cleans the whole text before
  splitting it into lines: redact (token and its Basic credential as extra secrets), drop control, format and
  line-separator characters, redact again (so a secret cannot be rebuilt by removing characters between its
  letters). The same cleaning applies to the summary line, and a last check refuses to send a body that still
  contains the token. Lines are cut at 4096 characters, at most 2000 lines are kept (first 1000 and the tail) and
  the body is capped at about 1 MiB (the first quarter and the tail of the run, with one "omitted" line). If
  Loki answers 400 or 413 to the push, ONE small follow-up push with just the `run_summary` line is made; any
  other failure is not retried.
- The 30 s limit can be lowered for tests with `COWLIC_SHIP_TIMEOUT`; only a positive integer up to 120 is
  accepted, anything else (including 0, which would mean no limit) falls back to 30.
- One `run_summary` line built from allowlisted, validated fields of `last-run.json` (parsed strictly: NaN and
  Infinity are rejected; if the file is unusable or is not from this run, no summary line and the run is
  unaffected): `run_summary exit_code=0 failure_class=none added=2 skipped=1 no_match=0 too_big=0 failed=0
  not_attempted=0 duration_s=159`. No playlist URLs, no log file path.
- Never sent: `.env` values, the token, URLs with queries, Telegram, D1 or Cloudflare ids. The URL, user and token
  are never printed or logged; redirects are not followed; environment proxies are ignored.

Labels (low cardinality, nothing per track or playlist): `job="cowlic-ingest"`, `host` (the machine host name),
`level` (`info`, `warn`, `error`; from a leading `ERROR` or `WARN` token of the line, default `info`). The
`run_summary` line is `info` for exit code 0 and `error` otherwise.

Find the logs in Grafana (Explore, Loki data source of your stack):

```logql
{job="cowlic-ingest"}                                  # all lines
{job="cowlic-ingest", level=~"warn|error"}             # problems only
{job="cowlic-ingest"} |= "run_summary" | logfmt        # one line per run; then filter, e.g. | exit_code != "0"
```

Set the time range to the last 24 hours or more (one run a day). Free tier retention is about 14 days. Whether
the first live push works is verified by the owner in #108.

### Run metrics (optional)

Added in #105, decided in #106. Right after the log push the same step sends ONE line of Influx line protocol
(plain text, `POST /api/v1/push/influx/write`, no new dependency) with a few numbers about the run. It needs two
more keys in `.env` (names only); the token is the same one, and it needs the `metrics:write` scope in addition:

- `INGEST_GRAFANA_PROM_URL`: the Prometheus (metrics) URL of your stack (`https://prometheus-....grafana.net`; the
  path `/api/v1/push/influx/write` is added if missing). Same rule: https and a `*.grafana.net` host only.
- `INGEST_GRAFANA_PROM_USER`: the metrics user (instance id; a different id from the Loki one).

Both absent: no metrics, nothing is logged about them. Only one of them, or the log step not configured: no
metrics. Failure isolation is the same as for the logs: the metrics push has its own 5 s request timeout and 15 s
deadline inside the wrapper's 30 s limit, is never retried, and its result only changes the one status line in
`ingest.log` (for example `grafana: loki push ok (HTTP 204); metrics push failed (HTTP 401)`). Because the log
push comes first, a log push that hangs for its full deadline can leave the metrics push without time; the next
run is the retry. The values come from the same validated fields of `last-run.json` as the summary line (no
summary, no metrics) and go through a last check that refuses a body containing the token.

Metrics are integer gauges (the value of the last run; they are not counters), tags `job="cowlic-ingest"` and
`host`; nothing per track, playlist or failure class as a label. The name is `cowlic_ingest_<field>` (Influx
measurement `cowlic_ingest`, one series per field, about 15 series in total; the final names are confirmed in the
first live push, #108):

| Metric | Meaning |
|---|---|
| `cowlic_ingest_last_run_timestamp_seconds` | `finished_at` of the run, Unix seconds |
| `cowlic_ingest_success` | 1 if the exit code was 0, else 0 |
| `cowlic_ingest_exit_code` | the wrapper exit code |
| `cowlic_ingest_duration_seconds` | finished minus started |
| `cowlic_ingest_added`, `_skipped`, `_no_match`, `_too_big`, `_failed`, `_not_attempted` | the run's counts (absent when ingest produced no result) |
| `cowlic_ingest_failure_partial`, `_config`, `_youtube_sign_in`, `_crash` | 1 for the failure class of the run, else 0 (all 0 means `none`) |

Find them in Grafana (Explore, Prometheus data source of your stack):

```promql
cowlic_ingest_success                                  # 1 after a good run, 0 after a failed one
time() - cowlic_ingest_last_run_timestamp_seconds      # seconds since the last run finished
cowlic_ingest_added                                    # tracks added by the last run
cowlic_ingest_failure_youtube_sign_in == 1             # runs stopped by the YouTube bot check
```

The metrics carry the push time, so a night without a run shows as a gap, not as a repeated old value; use
`last_run_timestamp_seconds` to tell. No alert rules are committed. Free tier: 10k active series, this uses about
15; metrics retention about 14 days.

#### Dashboard

`deploy/grafana/cowlic-ingest-dashboard.json` is a ready-to-import dashboard (#156). It holds no stack URL, id,
token or data source uid; the two data sources are import inputs.

Import: in Grafana go to Dashboards -> New -> Import, upload the JSON file, pick your stack's Prometheus data
source for `DS_PROMETHEUS` and the Loki one for `DS_LOKI`, then Import. Set the time range to 24 hours or more
(the default is 7 days): there is one run a day, so a shorter range shows nothing. The `Host` variable filters by
the `host` label.

Panels:

- Stat row, last run: `Last run` (OK or FAILED from `cowlic_ingest_success`), `Since the last run finished`
  (`time() - cowlic_ingest_last_run_timestamp_seconds`; yellow after 26 h, red after 50 h), `Exit code`, `Added`,
  `Failed`, `Duration`. They take the latest value of the last 14 days (the metrics retention); "No data" means
  more than 14 days without a run. Runs that write no `last-run.json` (locked out with 75, or an early 78) push
  nothing.
- The per-run panels below wrap each series in `last_over_time(...[$__interval])`: a run pushes one sample, and
  a plain range query would draw it only for its 5 minute lookback, so some runs would be missing at a coarse
  step.
- `Tracks per run`: added, skipped, no match, too big, failed and not attempted, one point per run.
- `Run duration` and `Success (1) / failure (0) per run`: one point per run.
- `Failure class of each run`: state timeline of `cowlic_ingest_failure_partial`, `_config`, `_youtube_sign_in`,
  `_crash`.
- Logs (Loki, `job="cowlic-ingest"`): `Run summary lines` (the `run_summary` line of each run), `Warnings and
  errors` (`level` warn or error) and `All run logs`.

The query names follow the table above and the log labels above; they were checked against `ingest/ship_grafana.py`
and this runbook, not against a live stack. The live import is confirmed by the owner (#108).

## Per-run limits and the local state file

Added in #119 (the base for the artist source in #121). Without the options below a run behaves as before:
no cap and no time limit. The wrapper forwards `--max-new`, `--max-minutes` and `--pace-seconds` only through
`COWLIC_INGEST_ARGS` (see "The cap and the 21:10 timer"); use the options directly when calling ingest by hand:

```sh
ingest/.venv/bin/python ingest/ingest.py --sources sources.yml --max-new 200 --max-minutes 90
```

| Option | Default | Meaning |
|---|---|---|
| `--max-new N` | none (no cap) | Stop after N tracks were attempted in this run. Counted: new, too big, failed (and dry-run matches). Not counted: tracks already in the library, tracks in cooldown, tracks with no match (no download was tried) |
| `--max-minutes M` | none (no limit) | Wall clock. No new track is started after M minutes (a download in progress finishes) |
| `--state-file PATH` | `${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/ingest-state.json` | Where the state is kept |
| `--retry-no-match-days D` | 30 | Do not retry a `no_match` track for D days (0 = retry every run) |
| `--retry-failed-days D` | 3 | Do not retry a `failed` track for D days (0 = retry every run) |

The limits are checked before each track that would be worked on and before each next playlist is fetched. When
one is hit the run ends normally (exit code unchanged: 0 unless something failed), logs one line
`stopped: cap|time: ...` with counts, and the next run picks up the rest (already stored tracks are skipped).

### State file

`ingest-state.json` sits next to the log in the state directory (outside the repo; `ingest-state.json` is also in
`.gitignore`). It remembers, per Spotify track id, that the track ended `no_match` or `failed` and the date
(UTC) after which it may be tried again:

```json
{"version": 1, "tracks": {"<22-char spotify id>": {"status": "no_match", "retry_after": "2026-11-09"}}}
```

- Tracks in cooldown are skipped without any YouTube work, logged as `skipped (cooldown, retry later): <label>`
  (the same label ingest logs for other skipped tracks), and do not use the cap.
- A track that later ends `new`, `exists` or `too_big` is removed from the state. A sign-in challenge is not a
  track failure and is not recorded. A `--dry-run` reads the state but never writes it.
- Only ids, a status word and a date are stored: no titles, artists or URLs. Mode 0600 (directory 0700).
- Written atomically after every change (temp file + fsync + rename), so a killed run keeps what it learned and a
  failed write leaves the previous file intact (one `WARN could not write the state file` line per run).
- A missing file starts empty. A corrupted or unknown file starts empty with `WARN state file is corrupted ...`
  and is replaced at the next change; malformed single entries are ignored with a WARN. Ingest never fails
  because of the state file. To reset all cooldowns, delete the file.

### Why a run stopped

The log ends every run with `stop reason: done|cap|time|sign-in` (a run that never started, such as a config
error, logs none). The same value is `stop_reason` in `last-run.json` -> `result` (`null` when nothing ran):

- `done`: all playlists were worked through. `cap` / `time`: a limit ended the run early. `sign-in`: the
  YouTube sign-in stop (exit 3).
- A playlist that was cut short by a limit has `status: "stopped"` and `not_attempted` = tracks not looked at;
  later playlists are `not_attempted`. A limit stop is not a failure: `failure_class` stays `none`.
- New count `cooldown` (per playlist and in `counts`): tracks skipped because of the state file.

## Pacing and backoff

Added in #120. Reason: yt-dlp documents about 300 videos per hour for a guest YouTube session and recommends a
5-10 s pause between downloads; running flat out (about 300-400 tracks an hour) is at that limit, so it is not
the default.

| Option | Default | Meaning |
|---|---|---|
| `--pace-seconds S` | 5 | Pause between downloads, counted from the end of the previous download (time spent on matching in between counts). 0 = no pause |
| `--pace-jitter-seconds J` | 5 | A random 0..J seconds is added to every pause, so the pause is 5-10 s by default. 0 = a fixed pause |
| `--retry-attempts N` | 3 | Attempts in total (1 try + 2 retries) for a transient error. 1 = no retry |

- The pause is only taken before a download: not before the first one of a run, and never for tracks that are
  already present, in cooldown, without a match, or in `--dry-run` (no download happens there). It counts as wall
  clock for `--max-minutes`. Waits use a monotonic clock.
- Backoff: after a transient error the wait doubles, 10 s then 20 s, never more than 120 s. Transient means HTTP
  429 or 5xx (also "Too Many Requests", "Service Unavailable", "Bad Gateway", "Gateway Timeout") in the error of
  a download, of the YouTube Music search (also network errors there) or of the Spotify playlist fetch. After the
  last attempt the error counts as an ordinary track failure (cooldown, see above) or a failed playlist.
- The YouTube sign-in challenge is never retried and is not paced around: the first one ends the whole run
  (exit 3, see below), the tracks and the cap are not touched afterwards, and what the state file learned before it
  stays. Nothing here bypasses a YouTube check (#62).
- These defaults also apply to plain playlist runs (the timer): a run is slower by 5-10 s per downloaded track
  after the first. Use `--pace-seconds 0 --pace-jitter-seconds 0` for the old speed.
- yt-dlp's own sleep options (`--sleep-interval` and friends) are not used: spotdl accepts yt-dlp options through
  `--yt-dlp-args` and passes them to its `YoutubeDL` object (read in spotdl's source, not run), but ingest starts
  one spotdl process per track, and yt-dlp's sleeps live inside one process, so the pause is done by ingest.

## Match-rate report (go/no-go gate for the artist ingest)

Before all followed artists are ingested (umbrella #102) we measure how often the matcher finds the right YouTube
Music video for an artist's tracks. The matcher was validated on only 12-22 tracks so far (assessment #92,
section 5); known weak spots are remasters, live versions, different durations and non-Latin names.
`ingest/match_report.py` measures it on a sample. It downloads and uploads nothing and touches no Telegram, D1,
cookies or Spotify user login: only Spotify catalog reads (Client Credentials, the `SPOTIFY_CLIENT_*` values the
ingest already uses) and YouTube Music searches.

```sh
ingest/.venv/bin/python ingest/match_report.py <artist id or URL> [<artist id or URL> ...] \
    [--artists-file artists.txt] [--max-tracks 150] [--min-interval 1.0] [--wrong N --checked M] [--out match-report-NAME.csv]
```

- Pick 5-10 artists that look like your library: some with remasters and live albums, some with non-Latin names.
  `--artists-file` takes one id or `https://open.spotify.com/artist/<id>` URL per line (`#` comments allowed). A
  value that is not a valid id or URL is refused, never rewritten.
- Per artist: tracks are listed, duplicate recordings collapsed (`dedupe.py`), then a deterministic sample is
  searched. `--max-tracks` (default 150, at most 1000) is a hard cap for the whole run, shared between the artists
  (budget an artist does not use goes to the next ones). It must be at least the number of artists.
- The search is throttled: at least `--min-interval` seconds (default and minimum 1) between two searches, bounded
  backoff on HTTP 429/5xx, at most 4 x `--max-tracks` searches, and a stop after 3 failed tracks in a row. A
  150-track run takes roughly 3-8 minutes. Do not run it repeatedly in a loop.
- Output: `match-report-<UTC time>.csv` in the current directory (or `--out`; the name must match
  `match-report*.csv`, which is gitignored; an existing file is never overwritten). UTF-8 with BOM, mode 0600.
  Titles are untrusted text: control characters are stripped and a cell starting with `=`, `+`, `-` or `@` gets a
  leading apostrophe, so the file is safe to open in a spreadsheet.
- Columns: `artist, artist_id, title, duration_s, matched_title, matched_duration_s, duration_delta_s, similarity,
  status, reason, pattern, reviewed`. Status: `matched`; `no_match` (the matched columns then show the closest
  result YouTube Music returned and `reason` is `title` (no result with title similarity >= 0.85), `duration` (the
  title fit but the duration was more than 3 s off) or `no_results`); `uncomparable` (no usable title or duration,
  not searched); `error` (the search failed after retries). `pattern` tags every `no_match` row (below);
  `reviewed` is empty: you fill it with `ok` or `wrong` for the matched rows you check.
- Failing patterns, one primary pattern per `no_match` row so the counts add up, first rule that fits wins, taken
  from the Spotify title: `remaster` (remaster, deluxe, anniversary, expanded, reissue, bonus track), `live` (live,
  acoustic, session, unplugged), `remix` (remix, rmx, edit, mix), `non-latin` (any letter outside the Latin script:
  Cyrillic, CJK, Arabic, Greek ...), `featuring` (feat, ft), `short-generic` (at most 2 words, or at most 6
  characters, or intro/outro/interlude/untitled/skit/prelude/reprise/theme), then by the search outcome:
  `duration-only` (the title was found, the duration was off), `no-results`, `other`.
- The summary on stdout gives the counts, the match rate overall and per artist, the share of search errors (with a
  loud WARNING above 10%: the verdict may be misleading), the failing patterns with counts, most frequent first,
  and the 15 lowest-similarity failures. Exit code 0 normally, 2 if an artist failed or the run stopped early (the
  CSV is still written), 1 for invalid input or missing credentials. A YouTube sign-in challenge in a search stops
  the run at once; it is not bypassed.

### How to read it

The gate is the one from assessment #92: **matched >= 80% AND wrong <= 2%**. Match rate = `matched / (matched +
no_match)` on the sample (`uncomparable` and `error` are left out; look at them separately, many of either means
the sample or the connection is bad). The script prints one of three verdicts:

| Verdict | Meaning |
|---|---|
| `matched rate >= 80%: go IF the owner's spot-check of matched rows finds <= 2% wrong` | Half the gate. Do the spot-check below; only if it holds, start the capped first run (#113) |
| `matched rate < 80%: no-go for now (tune the matcher; see the top failing patterns)` | Tune the matcher for the most frequent failing pattern, re-run the report on the same artists, compare |
| `no verdict` | Nothing was compared (all `uncomparable` or `error`) |

The wrong-match rate cannot be computed automatically (the script cannot know that a matched video is the wrong
recording). It comes from the owner: label a random sample of `matched` rows `ok` or `wrong` in the `reviewed`
column (open the matched video on https://music.youtube.com; a live or cover version that slipped through is
`wrong`; at least 50 rows is a sensible sample), wrong rate = wrong / checked, must be <= 2%. You can pass the counts
back (`--wrong N --checked M` on a re-run; it only prints the rate against the limit, it does not change the
verdict line). A sample under 30 compared tracks only gives a hint (the summary says so). Read the per-artist lines
too: one artist at 20% in an otherwise good sample (say, a non-Latin name) is a finding even when the overall rate
passes.

What the owner does (ticket #112): run the report on the chosen artists, open the CSV, filter `status = no_match`
and sort by `similarity` ascending (the stdout list is the same view). For a handful of rows check on
https://music.youtube.com whether a correct video exists. `reason = duration` with a right-looking video means the
tolerance is too tight or the Spotify release is a different edit; `reason = title` with a right-looking video means
the title normalisation misses something (remaster or live suffixes, transliteration); `no_results` means search,
not scoring, is the problem. Do the `matched` spot-check above. Then write the match rate, the wrong rate, the
decision and the top failing patterns in #112. Do not commit the CSV.

## Exit codes

| Code | From | Meaning | `failure_class` |
|---|---|---|---|
| 0 | ingest | Everything done; new tracks added, others already present | `none` |
| 1 | ingest | Some track or playlist failed, or sources/`.env` invalid | `partial` or `config` |
| 2 | ingest | Bad command-line usage (only when calling ingest directly), or `followed_artists` is enabled but the Spotify user login is missing or rejected (nothing ran; the result file, if requested, says `config`) | none written, or `config` |
| 3 | ingest | YouTube asked to sign in; rest of the run skipped | `youtube-sign-in` |
| 75 | wrapper | Another run holds the lock; nothing ran | not written |
| 78 | wrapper | A required prerequisite or the venv is missing, or (before the lock) `flock` is missing, the state dir is unusable, neither `HOME` nor `XDG_STATE_HOME` is set, or `cd` to the repo fails; nothing ran. `last-run.json` is written (class `config`) only for failures after the lock is taken (venv, prerequisite check); for the pre-lock failures none is written (flock missing: one ERROR line in the log if the state dir exists, otherwise stderr only) | `config` or not written |
| other | ingest | Crash, kill or timeout (for example 125 and above for signals) | `crash` |

When exit 1, read `failure_class` and the log to tell a partial failure from a configuration error.

## YouTube sign-in failure (exit 3)

YouTube answered a download with "Sign in to confirm you're not a bot". Ingest logs one clear `ERROR
youtube-sign-in` line, skips the rest of the run, does not retry, and exits 3. Tracks and playlists finished
before the challenge are already stored; the rest are picked up by the next run (ingest is idempotent).

What to do: wait and re-run later (the next timer run does that on its own). Nothing else.

What we do not do, by decision (#62): no cookies, no proxies, no PO-token tricks, no client rotation, no other
bypass of the bot check. They are fragile, against YouTube's rules, and cookies would put a Google account at
risk. If sign-in failures persist, tell the owner; do not work around them.

## Add a playlist

Edit `sources.yml` at the repo root and commit via a PR. Format: a flat list, one `- <url>` per line; blank
lines and whole-line `#` comments are allowed; nothing else (no inline comments, no other YAML).

```yaml
# my playlist
- https://open.spotify.com/playlist/<22 characters>
- https://open.spotify.com/album/<22 characters>
```

Allowed URLs: `https://open.spotify.com/playlist/<22 alphanumeric chars>` or `/album/<22 chars>`, optionally
with `?si=...`. The playlist or album must be public. Private playlists and Liked Songs are not supported. Any
invalid line fails the whole run (exit 1, class `config`). URLs are public data; never put secrets in the file.
Test a new entry with `ingest/.venv/bin/python ingest/ingest.py --sources sources.yml --dry-run`.

## Followed artists (all artists you follow, no playlist)

Added in #121. `sources.yml` can have a `followed_artists:` section. A run then also takes the artists the owner
follows on Spotify, lists each artist's releases and tracks, removes duplicate recordings across releases
(single + album + deluxe, `ingest/dedupe.py`) and feeds the result into the same per-track pipeline as a
playlist. Nothing is created or changed on Spotify: the login scope is `user-follow-read` (read only) and no
playlist is made anywhere. The D1 schema is unchanged.

**It is off until you add the section.** The repo's `sources.yml` has no such section, so the nightly timer run
behaves exactly as before until the owner opts in (a file without the section is parsed and run as before).

### Format

The playlist list stays as it is. The section is added next to it. Strict, dependency-free: indentation is exactly
two spaces for keys and four for the `exclude` items; whole-line `#` comments and blank lines are allowed; no
inline comments, no other YAML. Unknown or repeated keys, a bad value, a repeated id or a repeated section fail
the whole run with exit 1 (class `config`) before anything runs.

```yaml
# (commented example, put it at the end of sources.yml when you opt in)
# followed_artists:
#   enabled: true          # false turns the source off without deleting the section; default true
#   max_artists: 50        # artists started per run, 1..10000; default: no limit
#   rescan_days: 7         # a fully scanned artist is looked at again after this many days, 0..3650; default 7
#   groups: album,single   # Spotify release groups: album, single, compilation, appears_on; default album,single
#   market: US             # ISO 3166-1 alpha-2, upper case; default US
#   exclude:               # artist ids that are never ingested
#     - <22-character artist id>
```

(Remove the leading `# ` of every line of the section to enable it.) The first-run recipe below lists your followed
artists with their ids: `python ingest/spotify_user.py followed` prints `name<TAB>id`. A file with only the
section and no playlist is valid; a file with `enabled: false` and no playlist is not (nothing to do).
Not supported (deliberately, to keep the parser strict and small): exclude by name, a title filter (`skip_titles`),
other keys.

### What a run does

1. Reads the followed list (read scope, token refreshed from `SPOTIFY_USER_REFRESH_TOKEN`). Repeated artist ids
   across pages are dropped. Ids on `exclude` are skipped.
2. Playlists first (if any), then the artists in the order Spotify returns them. An artist scanned to the end
   less than `rescan_days` ago is skipped without any Spotify call.
3. Per artist: enumerate releases and tracks (Client Credentials, the same `SPOTIFY_CLIENT_ID/SECRET`), collapse
   duplicate recordings (earliest release wins; a recording already stored under another release is not
   downloaded again), then the normal per-track pipeline: already stored tracks are skipped, tracks in cooldown
   are skipped, the rest are matched, downloaded (paced, with backoff) and stored.
4. When the artist's last track was handled the day is recorded in the state file (`"artists"` key, ids and
   dates only). An artist stopped by the cap, the time limit or a sign-in challenge is not recorded: the next run
   starts there again and adds only what is missing. A track that failed in an otherwise complete scan is
   retried through its normal cooldown (`--retry-failed-days`) when the artist is rescanned, not by the artist
   record. An artist whose enumeration fails (Spotify error) is logged (`ERROR artist <name>: enumeration
   failed`), counted, not recorded (so the next run retries it), the others go on, and the exit code is 1.
5. Everything from the sections above applies unchanged: `--max-new` (shared with the playlists), `--max-minutes`,
   cooldowns, pacing and backoff, and the YouTube sign-in stop (the first challenge ends the whole run, exit 3).
   `max_artists` counts artists started in this run (including one whose enumeration failed), not the skipped
   ones, so a run with `max_artists: 50` works through the list in batches over several nights.

The log gets one line per artist with counts (`artist <name>: listed N, unique N, dropped N` and the result) and
one `followed artists: ...` line; `last-run.json` -> `result` gets a `followed_artists` object (status, track
counts and artist counts `listed, excluded, skipped_recent, deferred, scanned, failed, not_started`) and its
track counts are part of `counts`. Counts only: no artist id or name is written there.

### First run (owner steps, #111 and #113)

Not run by the author (no network or login was used while building this). Do these on the laptop, in order:

1. Once: `ingest/.venv/bin/python ingest/spotify_user.py login`, open the printed URL, approve (scope
   `user-follow-read`). The refresh token is stored in `.env` as `SPOTIFY_USER_REFRESH_TOKEN` (valid about 6 months;
   run `login` again on `invalid_grant`). This is the owner ticket #111.
2. Optional check: `ingest/.venv/bin/python ingest/spotify_user.py followed` lists the followed artists and the total.
3. Add the section to `sources.yml` (see above) in a branch/PR with `max_artists` small, e.g. 5, and the ids of
   artists you do not want in `exclude`.
4. Dry run, nothing is downloaded or stored and the artist state is not written (it still reads D1, Spotify and
   YouTube Music, and counts matches against the cap):
   `ingest/.venv/bin/python ingest/ingest.py --sources sources.yml --dry-run --max-new 20 --state-file /tmp/dry-state.json`
5. First capped run by hand: `ingest/.venv/bin/python ingest/ingest.py --sources sources.yml --max-new 50 --max-minutes 60`,
   then read the log and `last-run.json`-style result (`--result-json /tmp/r.json`). Repeat; a re-run adds only new
   tracks. This is the owner ticket #113.

Without `SPOTIFY_USER_REFRESH_TOKEN` (or with a rejected one) the run does nothing: it logs one `ERROR` line with
the fix and exits with code 2 (class `config`; before any playlist is run, so fix the login or set
`enabled: false`). A sources file without the section never needs the login.

### The cap and the 21:10 timer

Without `COWLIC_INGEST_ARGS` the timer run has NO cap and no time limit: with the section enabled and a big
followed list, the first night would try to ingest everything (about 5-10 s pause plus the download per track; the
unit stops at `TimeoutStartSec=2h`, and a run killed by that timeout writes no `last-run.json`). Set the caps in
the unit, or set `max_artists` small in `sources.yml`. Progress is kept either way: a killed run keeps what the
state file and D1 already hold, and the next run continues.

`scripts/run-ingest-local.sh` appends the optional environment variable `COWLIC_INGEST_ARGS` to the ingest command
line, for example in the unit (`systemctl --user edit cowlic-ingest.service`, or the commented line in
`deploy/systemd/cowlic-ingest.service`):

```ini
Environment="COWLIC_INGEST_ARGS=--max-new 200 --max-minutes 90"
```

Rules (a violation ends the run with exit 78 before ingest starts, with one log line that does not repeat the value):

- Unset, empty or blank: nothing is appended, the command line is exactly as before.
- Only these options, each followed by a separate positive integer (no leading zero, no `=` form, each at most
  once): `--max-new` (1..1000000), `--max-minutes` (1..1440), `--pace-seconds` (1..3600). Words are separated by
  spaces. Every other ingest option (`--state-file`, `--dry-run`, ...) is rejected on purpose.
- Only letters, digits, spaces and `-` may appear at all, at most 200 characters. The value is split with
  `read -a`: no `eval`, no shell expansion, no globbing. No secret belongs in it.
- `max_artists` is a `sources.yml` key (see above), not an ingest option, so it is not accepted here.

With a cap of 200 a night, about 2,500 tracks take about 13 nights and 40,000 about 200.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Exit 78, log says prerequisites check failed | Read the `MISSING` lines in the log; fix as hinted by `scripts/check-local-prereqs.sh` |
| `WARN deno` in the log | deno missing or older than 2.3.0; optional, install it if YouTube challenge errors appear |
| Exit 75 | A run is active; wait. If none is running, check `ps` for a leftover `run-ingest-local.sh` |
| `missing env vars` in the log | A key from the list above is absent or empty in `.env` |
| Exit 1, `.env` syntax error | Fix the line number named in the log; rules in Prerequisites (no `export`, no spaces around `=`, no inline ` #`, no duplicate keys) |
| Timer shows no NEXT | Not enabled: `systemctl --user enable --now cowlic-ingest.timer`; check the `daemon-reload` and `verify` steps |
| Unit fails immediately with status 203 or 200 | Wrong repo path in `WorkingDirectory` / `ExecStart`; fix both, `daemon-reload` |
| `ffmpeg` not found only under systemd | The unit's `PATH` lacks its directory; extend the `Environment=PATH=` line |
| Timer did not run last night | Machine was off or you were logged out; see Persistent and linger above; check `finished_at` in `last-run.json` |
| Exit 2, log says `SPOTIFY_USER_REFRESH_TOKEN is not set` or `invalid_grant` | `followed_artists` is on but there is no valid Spotify login: `ingest/.venv/bin/python ingest/spotify_user.py login` (once; valid about 6 months), or set `enabled: false` in the section |
| `ERROR artist <name>: enumeration failed` | Spotify error for that artist (rate limit, 5xx, network); exit 1, the others went on, the artist is retried by the next run |
| `Spotify request failed: HTTP 403 on /v1/...: <reason>` (also from `match_report.py` and `artist_tracks.py`), or `Spotify refused the request (HTTP 403)` ... `Spotify said: <reason>` (from `spotify_user.py followed`) | Read `<reason>`: it is Spotify's own `error.message` (cleaned, cut to about 200 characters, never the token or the URL query). Typical causes: the app is in Development Mode and the app owner has no active Spotify Premium (see #92), the Spotify account is not added under User Management in the developer dashboard, or the endpoint is not available to the Client Credentials token. Fix the cause in the Spotify developer dashboard; a retry does not help |
| Track skipped as `no_match` or `too_big` | No acceptable YouTube Music match (title and duration within 3 s), or the mp3 exceeds Telegram's 20 MB limit (about 10 minutes at 256k) |

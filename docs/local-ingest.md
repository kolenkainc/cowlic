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
Nothing about ingest is stored in GitHub (the old `ingest` environment secrets are removed in #75; the GitHub environment secrets that exist belong to the Worker deploy only). The repo is
public: never paste values into issues, PRs or logs. Every value that ingest loads from `.env` is hidden from the
spotdl child process by construction (the child gets the outer environment only, whatever a key is called; a variable
already set in the outer environment keeps its outer value and still passes, except the fixed list of known secret
names in `SPOTDL_HIDDEN_ENV`, which is always hidden).

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

### Optional hardening and the 1 hour limit

- `TimeoutStartSec=1h` stops a hung run. The unit also caps a run at `--max-new 30 --max-minutes 30` (see "The cap
  and the 21:10 timer"), so 1 h is a margin for one stuck download. A big backlog or the Last.fm ranking does not
  belong to the timer: use the bootstrap command (`docs/bootstrap-ongoing.md`). Changing the unit in the repo has no
  effect on the machine until you copy it again (owner step, #175) and run `systemctl --user daemon-reload`.
  A run killed by the timeout writes no
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
no retry loop: the next run is the retry, a failed night is a gap in Grafana (the only repeat is one small
summary-only push after an HTTP 400/413 of the full push, see below).

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

### Watch the Last.fm report live (`scripts/run-lastfm-report.sh`)

The match-rate report (`ingest/lastfm_report.py`, see below) bypasses the nightly wrapper. To watch it in Grafana
while it runs, start it through the report wrapper; every argument goes to the report unchanged:

```bash
scripts/run-lastfm-report.sh --lastfm-user <your-name> --years 5 --artists 100
```

Why a second wrapper and a few flags on the shipper (`--job`, `--logs-only`, `--offset-file`, `--final`,
`--filter`), not only a flag: the report needs its own lock, log, background loop and signal handling, which do not
belong in the nightly wrapper, whose path stays byte-identical. The shipper stays the one place for validation,
redaction, cleaning and the final guard.

- Output (stdout and stderr) goes through the shipper's `--filter` (same redaction and cleaning as a push) into
  `${XDG_STATE_HOME:-~/.local/state}/cowlic/lastfm-report.log` (mode 0600) and to
  the terminal. Every run starts a FRESH log: a non-empty log of an earlier run moves to `.1` (older ones shift;
  `COWLIC_LOG_KEEP` files kept, default 5), so a run killed mid-line never leaves the next one an offset in the middle of
  a line. The report's exit code is the wrapper's; 128+N if interrupted by signal N.
- Every `COWLIC_REPORT_SHIP_INTERVAL` seconds (default 20) the new complete lines (at most 256 KiB per push, offset
  kept in `.lastfm-report.offset`; a line longer than a batch is shipped cut at the batch size and its rest is
  skipped, never split over two pushes; a corrupt offset file is reset with a status line) are pushed with the label `job="cowlic-report"`, and once more at the end. The
  same failure isolation as the nightly: 30 s hard limit per push, no retry loop (a failed batch is simply sent
  again with the next one), an unreachable Grafana or a bad token never changes the exit code. Only the final push
  prints one status line.
- Logs only: no metrics and no `last-run.json`, so the nightly panels (`Last run`, metrics, `run_summary`) keep
  meaning the 21:10 run only.
- Safety: the user name is taken from `--lastfm-user` and handed to the filter and the shipper through the
  environment (`COWLIC_EXTRA_SECRET`, never argv); it is removed from the local log and from every shipped line.
  Residual risk, accepted: the filter works line by line, so a user name wrapped over a newline is not matched (the
  report prints no user name). A very short user name makes the final guard refuse every body (nothing is shipped;
  the loop prints one warning on the terminal), and the redaction mangles the log wherever those letters occur.
  The arguments are never echoed, logged or shipped. `LASTFM_API_KEY` is redacted as well (the shipper loads `.env`
  itself; the shell never reads it). The lines carry artist and track names, i.e. your own listening data, sent to
  your own Grafana Cloud, as the nightly log already does.
- Stopping: the report and the background loop run in their own process groups and are stopped by a trap when the
  wrapper ends or gets SIGINT, SIGTERM or SIGHUP. After a `kill -9` of the wrapper no trap can run: the report keeps
  running (stop it yourself), but the live loop checks that the wrapper is alive before every one-second sleep step and
  before every push, and ends by itself about a second later (no further pushes, no race with the next run on the
  offset file). A second report while one runs exits with 75. The shipper refuses `--logs-only` with the nightly job
  label `cowlic-ingest`.

Explore (Loki data source): `{job="cowlic-report"}`; the dashboard has a `Last.fm report (live, logs only)` panel
for it. Set the time range to the last 15 minutes while it runs.

### Watch a bootstrap session live (`scripts/run-bootstrap-local.sh`, #244)

`scripts/run-bootstrap-local.sh run` and `plan` (the bootstrap command, [bootstrap.md](bootstrap.md)) ship their session
log to the same Loki with the same shipper options as the report wrapper (`--logs-only --job cowlic-bootstrap
--offset-file`, a final `--final` flush) and the same three keys (`INGEST_GRAFANA_LOKI_URL`, `INGEST_GRAFANA_LOKI_USER`,
`INGEST_GRAFANA_TOKEN`; no new secret). Logs only: no metrics, no `run_summary`, no `last-run.json`, so the nightly panels
keep meaning the 21:10 run. It ships the whole session log, like the nightly ships `ingest.log`: the engine's lines with
artist and track names, redacted (`LASTFM_API_KEY`, the token, `COWLIC_EXTRA_SECRET`). Every `COWLIC_BOOTSTRAP_SHIP_INTERVAL`
seconds (default 20) and at the end; best effort with a 30 s limit (`COWLIC_SHIP_TIMEOUT`), it never changes the exit
code, the shared lock, the 21:10 guard or the signal handling. Locked (75), refused (78) or usage-failed invocations,
`rank` and `status` ship nothing. Explore: `{job="cowlic-bootstrap"}`; details and the dashboard proposal in
[bootstrap.md](bootstrap.md) section 5.

Bootstrap sessions can also run on a schedule, every 2 hours, from a second user timer (`cowlic-bootstrap.timer`, #279):
see [bootstrap.md](bootstrap.md) section 12. It shares this lock and the 21:10 guard with the nightly run.

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
| `cowlic_ingest_new_releases_checked`, `_with_new`, `_added`, `_failed` | integer counts from `result.new_releases`; absent when the phase is disabled or the object is missing; also included as `new_releases_<field>` in `run_summary` |
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
- `New releases per run`: tracks added and artists with new tracks (`with_new`), next to `Tracks per run`. No points are sent for runs without `result.new_releases`.
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
  `reviewed` is empty: optional, you may fill it with `ok` or `wrong` for the matched rows you check (labelling is
  optional tooling, not a gate; the owner chose not to label, see "How to read it").
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

The gate of assessment #92 reads **matched >= 80% AND wrong <= 2%**. Only the matched half is measured by code; the
wrong half needs labels, which are optional tooling, not a gate: the owner decided on 2026-10-11 (#214) not to label,
so the wrong-recording rate is an accepted, unmeasured risk (mitigations and how to reverse it:
[bootstrap.md](bootstrap.md), "Accepted risk"). Match rate = `matched / (matched +
no_match)` on the sample (`uncomparable` and `error` are left out; look at them separately, many of either means
the sample or the connection is bad). The script prints one of three verdicts:

| Verdict | Meaning |
|---|---|
| `matched rate >= 80%: go IF the owner's spot-check of matched rows finds <= 2% wrong` | Half the gate. The spot-check below is optional tooling, not a blocker (the owner chose not to label, #214); if someone does it and it finds more than 2% wrong, that is a finding to decide on |
| `matched rate < 80%: no-go for now (tune the matcher; see the top failing patterns)` | Tune the matcher for the most frequent failing pattern, re-run the report on the same artists, compare |
| `no verdict` | Nothing was compared (all `uncomparable` or `error`) |

The wrong-match rate cannot be computed automatically (the script cannot know that a matched video is the wrong
recording). It can only come from a human, and only if someone chooses to measure it (optional; the owner chose not
to): label a random sample of `matched` rows `ok` or `wrong` in the `reviewed`
column (open the matched video on https://music.youtube.com; a live or cover version that slipped through is
`wrong`; at least 50 rows is a sensible sample), wrong rate = wrong / checked, should be <= 2%. You can pass the counts
back (`--wrong N --checked M` on a re-run; it only prints the rate against the limit, it does not change the
verdict line). A sample under 30 compared tracks only gives a hint (the summary says so). Read the per-artist lines
too: one artist at 20% in an otherwise good sample (say, a non-Latin name) is a finding even when the overall rate
passes.

What to do with the report: run the report on the chosen artists, open the CSV, filter `status = no_match`
and sort by `similarity` ascending (the stdout list is the same view). For a handful of rows check on
https://music.youtube.com whether a correct video exists. `reason = duration` with a right-looking video means the
tolerance is too tight or the Spotify release is a different edit; `reason = title` with a right-looking video means
the title normalisation misses something (remaster or live suffixes, transliteration); `no_results` means search,
not scoring, is the problem. The `matched` spot-check above is optional. Then write the match rate, the wrong rate, the
decision and the top failing patterns in the ticket that asked for the report. Do not commit the CSV.

## Exit codes

| Code | From | Meaning | `failure_class` |
|---|---|---|---|
| 0 | ingest | Everything done; new tracks added, others already present | `none` |
| 1 | ingest | Some track or playlist failed, or sources/`.env` invalid | `partial` or `config` |
| 2 | ingest | Bad command-line usage (only when calling ingest directly), or `followed_artists` is enabled but the Spotify user login is missing or rejected, or `lastfm_artists` is enabled but `LASTFM_API_KEY` is missing or Last.fm refuses the ranking (nothing ran; the result file, if requested, says `config`) | none written, or `config` |
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

The committed unit sets `COWLIC_INGEST_ARGS=--max-new 30 --max-minutes 30` and `TimeoutStartSec=1h`, so the 21:10 run
stays lean (the playlist flow takes about 1 minute). Without `COWLIC_INGEST_ARGS` (for example a hand run, or a unit
copy from before #174) a run has NO cap and no time limit; a run killed by the unit timeout writes no `last-run.json`.
Progress is kept either way: a killed run keeps what the state file and D1 already hold, and the next run continues.
Changing the unit in the repo has NO effect on the owner's machine until the owner copies it (owner step #175).

Guard: the wrapper refuses to run when `sources.yml` has a whole-line `lastfm_artists:` key, even with
`enabled: false` (a plain grep, `enabled` is never read; only commented-out lines pass). It prints one ERROR line to
stderr and exits 78 before ingest starts and before the log or `last-run.json` is written. The ranking belongs to the
bootstrap command, see `docs/bootstrap-ongoing.md`: remove or comment out the section.

`scripts/run-ingest-local.sh` appends the optional environment variable `COWLIC_INGEST_ARGS` to the ingest command
line, for example in the unit (`systemctl --user edit cowlic-ingest.service`, or the line in
`deploy/systemd/cowlic-ingest.service`):

```ini
Environment="COWLIC_INGEST_ARGS=--max-new 30 --max-minutes 30"
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

## Last.fm match-rate report (the gate before the Last.fm source)

Before any Last.fm track is downloaded (umbrella #102, ticket #153) measure how well the Last.fm path would work.
`ingest/lastfm_report.py` is the sibling of `match_report.py` above, with the same CSV rules, throttle and gate
(#92: matched >= 80% AND wrong <= 2%). It makes read-only Last.fm calls and YouTube Music searches only: no
download, no upload, no Telegram, no D1 write, no cookies, no Spotify. It is NOT part of the 21:10 run and does not
touch `sources.yml`: the nightly timer must not use the `lastfm_artists` section (see the next section); the bootstrap
that does the real first run comes in Release 3 (#163), and this report was the go / no-go input before its first
session (#154 closed as GO on the matched rate, 2026-10-11). Its labelling step is optional tooling, not a gate: the
owner decided not to label (#214), see the runbook [bootstrap.md](bootstrap.md).

Setup: `LASTFM_API_KEY=<key>` in the local `.env` (name only here; never in a command line, a log or the CSV). The
Last.fm profile must be public (listening data private gives error 17, a clear message, no CSV). The user name is
given on the command line and is not printed.

```sh
ingest/.venv/bin/python ingest/lastfm_report.py --lastfm-user <name> \
    [--years 5] [--artists 100] [--sample-artists 10] [--max-tracks 150] [--max-minutes 45] \
    [--min-interval 1.0] [--check-stored] [--out match-report-lastfm-NAME.csv]
ingest/.venv/bin/python ingest/lastfm_report.py --summarize match-report-lastfm-NAME.csv   # offline, optional, after labelling
```

What a run does, in order:

1. Ranks the top `--artists` artists (default 100) of the last `--years` (default 5) from the weekly charts: about 53
   calls per year, about 261 for 5 years, 1 call per second, so about 5 minutes before anything else happens.
2. Takes `--sample-artists` of them (default 10, at most 50) evenly spaced over the ranking (ranks 1, 11, 21, ...),
   so the sample covers the head and the tail of the 100, not only the favourites. Enumerates their tracks (the 20
   most popular albums per artist, at most 30 Last.fm calls each) and collapses duplicate recordings.
3. Takes a deterministic sample of at most `--max-tracks` tracks (default 150, at most 1000) shared between the
   artists and matches each one the way the pipeline would: with the Last.fm duration (+-3 s) when Last.fm has one,
   by title and artist only when it has none (the title-only mode).
4. With `--check-stored` it also reads the stored tracks from D1 (one parameterless read-only SELECT, the same one
   the pipeline uses for the cross-source dedupe) and marks sampled tracks that are already stored under another id
   (`stored`; those are not searched and are left out of the match rate). It needs `CLOUDFLARE_ACCOUNT_ID`,
   `D1_DATABASE_ID` and `INGEST_CLOUDFLARE_API_TOKEN` in `.env` (the ingest token is write-capable, which is why the
   check is opt-in and read-only by construction). Without them, or when D1 fails, the report prints one
   `stored check: skipped (...)` line and goes on; it never fails because of it.

Hard caps: `--max-tracks` tracks, so at most 4 x that many YouTube searches (the search budget, a time cap and the
failure stops below end the run early: the CSV is still written and the exit code is 2); `--max-minutes` wall time
(default 45, at most 240; checked between tracks and artists, so the ranking itself, about 5 minutes, always completes
unless the call budget stops it); a Last.fm budget (ranking bound plus 30 calls per sampled artist, shown in the
summary): if the ranking hits it the run ends with exit 1 and NO CSV (nothing was matched yet), while the 30-call
budget of one artist only makes that artist fail (counted as a failed artist, the others go on); at least 1 s between
two searches (`--min-interval`, never faster); a stop after 3 failed searches in a row or 3 failed artists in a row.
A YouTube sign-in challenge stops the run at once and is not bypassed (no cookies, proxies, PO-token tricks or client rotation, decision #62). With the
defaults a run takes roughly 15-25 minutes. Do not run it in a loop.

Output: `match-report-lastfm-<UTC time>.csv` in the current directory (the name must match `match-report*.csv`, which is
gitignored; an existing file is never overwritten; UTF-8 with BOM, mode 0600). Titles are untrusted text: control
characters are stripped and a cell starting with `=`, `+`, `-` or `@` gets a leading apostrophe. Do not commit it.
Columns: `artist_rank, artist, title, lastfm_duration_s, mode, matched_title, matched_artists, matched_duration_s,
duration_delta_s, similarity, status, reason, pattern, reviewed, run_stopped`. `run_stopped` is empty for a run that
finished and holds the stop reason (same on every row) otherwise, so `--summarize` knows the run was partial.

- `mode` is `duration` or `title-only` (the mode the track was tried in). `status` is `matched`, `no_match` (the
  matched columns then show the closest YouTube result), `stored` or `error`. `reason` for a `no_match` row: `title`
  (nothing similar enough), `duration` (duration mode: right title, duration off by more than 3 s),
  `artist_variant_or_length` (title-only mode: the title fit but the artist, a live/remix/cover word or a length
  outside 60-600 s rejected it), `no_results`. `pattern` is the same failing-pattern tag as in the Spotify report.
- Title-only matches have no duration check, so they are the risky ones: `matched_artists` and `matched_duration_s`
  are there for the eyeball check. Thresholds of that mode (`ingest/match.py`): title similarity >= 0.93, artist
  similarity >= 0.7 against any returned artist name (a leading the/a/an is ignored on both sides; the 0.7 is a judgement, not measured, so judge it on the numbers), candidate length
  60-600 s, and none of the variant words (live, remix, edit, acoustic, cover, ...) that the requested title lacks.
  The duration mode keeps title >= 0.85 and +-3 s.
- Relaxed title pass (#194), both modes: only when the strict pass finds nothing in the same search results (no extra
  search call), `find_match` compares the titles again after dropping version noise that does not change the recording
  (` - Remastered YYYY`, `(Remastered YYYY)`, `(YYYY)`, `(Album Version)`, `(Single Version)`, `[Explicit]`), reading `&`
  as `and`, ignoring Latin diacritics, apostrophes (titles of 2+ words only: `Hell` and `He'll` stay apart) and a plural
  `s` on the last word of a title of 2+ words. The variant markers (live with its venue and year, acoustic, remix,
  instrumental, karaoke, cover, sped up, slowed, 8D, reverb, radio edit, extended, demo, ...) of the two titles must be
  the same, however spelled or ordered. The thresholds and the +-3 s are unchanged. Titles over 300 characters are left
  to the strict pass. A match that only this pass accepted is logged "(matched after title normalization)" and counted
  as `matched_normalized` (summary line and result JSON, only when non-zero). There is deliberately no prefix rule
  (`Bye Bye` / `Bye Bye Bye`): the duration path has no artist veto, so a prefix could ingest another song; the chant
  class (`Ooh La` / `Ooh La La La`) is dropped for now (follow-up idea: allow it only with a matching artist).

The first line of the output is the verdict (the line you copy into #154), followed by its warnings. It is a go only
on a sample the gate can be judged on. Otherwise it reads `no verdict: <reasons>` (and never says go) when any of
these holds: fewer than 30 compared tracks (`matched + no_match`); more than 10% of the searches failed; the run
stopped early (sign-in challenge, search or time budget, 3 failures in a row, interrupted; `--summarize` reads this from
the CSV); or a mode with matched rows has fewer than min(30, its matched rows) rows labelled `ok`/`wrong`. When none of
them holds: matched < 80% is `no-go`, a wrong rate above 2% overall or in a mode is `no-go`, else `go`. The tool prints
`no verdict` for a CSV without labels; that is how the tool works and not a blocker for bootstrap, because labelling
is optional tooling and not a gate (the owner chose not to label, #214).

The rest of the summary on stdout gives: artists ranked and sampled; tracks listed and unique; the share WITH and WITHOUT a
Last.fm duration (how often Last.fm gives none, overall and in the sample); Last.fm calls and YouTube searches
against their budgets; the stored check line; matched / no_match / stored / error overall and per mode, so the
title-only matches are never mixed into the duration ones; the match rate overall and per mode
(`matched / (matched + no_match)`); the wrong rate overall and per mode; the failing patterns; the 15
title-only matches with the lowest title similarity; the 15 lowest-similarity failures; and a per-artist table.
Exit code 0 normally, 2 if the run stopped early or an artist failed (the CSV is still written), 1 for invalid input
or a missing or malformed key.

How to label and read it (optional tooling, not a gate; the `--summarize` command still works if someone labels):

1. Open the CSV. In the `reviewed` column write `ok` or `wrong` for matched rows: open the matched video on
   https://music.youtube.com (a live, remix or cover version, or another artist's track with the same title, is
   `wrong`). Label EVERY `title-only` matched row if there are fewer than about 50, else a random 50 of them, and a
   random 50 of the `duration` matched rows. Any other value is counted as unrecognised and ignored; rows that are
   not `matched` are ignored.
2. Run `--summarize` on the labelled file (offline, needs no key). It prints the same figures with the wrong rates:
   `wrong / (ok + wrong)` overall, for `duration` and for `title-only`. The title-only mode is judged on its own
   wrong rate: it should be <= 2% by itself (if labelled) even when the overall rate is fine. A mode with matched rows and too
   few labels (under min(30, its matched rows)), a sample under 30 compared tracks, a partial run or many failed
   searches give `no verdict: ...` (see above), not a go. Rate below 80% with a good wrong rate means tuning the matcher by the most
   frequent failing pattern; a high wrong rate in the title-only mode means raising its thresholds or turning it off.
3. Write the numbers and the decision in the ticket that asked for them (#154 is closed). Check also: how many tracks Last.fm gives no duration for (a high share
   means most matches are title-only), and the per-artist lines (one artist at 20% in an otherwise good sample is a
   finding).

Last.fm terms followed by this report and by the source (ingest/lastfm.py, clause numbers [R], to be re-checked
against https://www.last.fm/api/tos): non-commercial use only (this is a private library); no use of Last.fm audio,
images or artwork (names, playcounts, album and track names only; the report never reads covers); credit Last.fm with a
link wherever its data is shown to other people; caching follows the HTTP headers (nothing is cached); at most 1
call per second with bounded backoff. The 5-year window is the sum of the weekly artist charts that lie completely
inside it (`user.getWeeklyChartList` + one `user.getWeeklyArtistChart` per week), because the other methods have no
from/to range.

## Last.fm artists (your most listened artists, no playlist)

Added in #152 (the report that gates it is the section above). `sources.yml` can have a `lastfm_artists:` section. A run
then also takes your top artists by listening over the last years from Last.fm and ingests their tracks through the
same pipeline as a playlist (cap, state file, pacing and the YouTube sign-in stop apply unchanged). It is opt-in:
a file without the section behaves exactly as before. Since #174 the nightly wrapper (`scripts/run-ingest-local.sh`)
refuses any `sources.yml` that has the key, also with `enabled: false` (exit 78); `enabled: false` only helps when
calling `ingest.py` by hand. It may be combined with playlists
and with `followed_artists`; the playlists run first, then the followed artists, then the Last.fm artists.

```yaml
# lastfm_artists:
#   enabled: true         # default true
#   user: <last.fm user>  # required when enabled: 2-15 letters, digits, _ or -, not a URL
#   years: 5              # listening window, 1..10; default 5 (about 53 weekly-chart requests per year)
#   max_artists: 100      # size of the ranking, 1..10000 (Last.fm client limit: 1000); default 100
#   rescan_days: 7        # a fully scanned artist is looked at again after this many days, 0..3650; default 7
#   exclude:              # artist names, compared case-insensitively without punctuation; one per line, as is
#     - Various Artists
```

Same strictness as `followed_artists`: two-space keys, four-space list items, unknown or repeated keys, inline
comments and quotes are errors. The API key is NOT a `sources.yml` key: set `LASTFM_API_KEY` in `.env` (never
logged, and hidden from the spotdl child process). A missing or malformed key ends the run with exit 2 before any work
(`config`). The ranking is read AFTER the playlists and followed artists, so a Last.fm problem never stops them; it is
not cached (Last.fm caching follows response headers, which the client does not read) and `--max-minutes` bounds it
too. If Last.fm refuses it for a reason only you can fix (unknown user, invalid or suspended key, private or empty
profile: error codes 6, 10, 17, 26) the playlists are still ingested and reported, then the run exits 2 (`config`);
an outage, 5xx or rate limit, or a D1 error while reading the stored rows, is a failed Last.fm phase (exit 1, result
status `failed`). A per-artist failure is counted (exit 1) and the others go on. The result
JSON gets an additive `lastfm_artists` object (counts only, including `dropped_stored`); title-only matches are
counted in `matched_title_only` and logged as `(matched by title only)`.

The 21:10 nightly timer must not use the `lastfm_artists` section (the first run is slow and unbounded by default);
the wrapper enforces it (#174, see "The cap and the 21:10 timer"): the `lastfm_artists:` key must be absent or
commented out in the nightly `sources.yml`, `enabled: false` is not enough. The bootstrap process (#163, #168) uses its
own config, a separate local file outside the repo. Do not add the section to the nightly `sources.yml`
to try it: measure first with the report above, and take the dry run and the first capped session from the bootstrap
runbook, [bootstrap.md](bootstrap.md) (#170).

Ids and dedupe: Last.fm has no track ids, so a track gets `lfm:` + the first 16 hex digits of a SHA-256 of the
normalised artist and title (the album is not part of it, so one title on two albums is one track). A track whose
artist, title and duration (+-3 s; artist and title only when a side has no duration) are already stored under another
id, say from a Spotify playlist, is dropped before any search (counted as `dropped_stored`). A track without a Last.fm
duration is matched in the title-only mode (thresholds in the report section), and counted as `matched_title_only`.
Terms of use of the Last.fm data and the no-bypass rule for the YouTube bot check: see the report section and
"YouTube sign-in failure (exit 3)".

Known limitations:

- Compilation tracks do not match the stored rows (Last.fm side compares on `artists[0]`, the stored row on the album
  artist), so such a track can be stored twice: once from a playlist, once from Last.fm.
- Last.fm gives few track durations, so most tracks are matched by title and artist only (no duration check): these
  matches are weaker than the playlist matches and are counted separately.
- The first run can be slow: the ranking alone is about 261 weekly-chart calls at 1 call per second for 5 years,
  then one or more calls per album of every artist, again at 1 call per second, before any download. Bound the
  nights with `COWLIC_INGEST_ARGS` (`--max-new`, `--max-minutes`, `--pace-seconds`, see above) and a small
  `max_artists`; progress is kept in the state file (`artists` map, keys `lfm:<16 hex>`) and the next run continues.

## New releases of known artists (nightly, bounded)

Added in #172 (contract: [bootstrap-ongoing.md](bootstrap-ongoing.md) section 4). An opt-in `new_releases:` section of
`sources.yml` makes a run check, AFTER the playlists (and the other sections), the artists of the stored bootstrap
ranking (`bootstrap-ranking.json`, computed by the bootstrap command only, never by this run) for releases that are
not in the library, and ingest their tracks through the same pipeline as a playlist. A file without the section, or
with `enabled: false`, behaves exactly as before. The committed `sources.yml` enables the owner-approved bounded
check (#165, #175), after the partial-release retry fix (#238) and metrics (#173) landed.

```yaml
new_releases:
  enabled: true        # false disables the phase; default true
  max_artists: 10      # due artists checked per night, 1..1000; default 10
  recheck_days: 7      # a checked artist is due again after this many days, 1..365; default 7
  max_new: 20          # tracks added by this phase per night, 1..1000; default 20
```

Same strictness as the other sections (unknown or repeated keys are errors; `lookback_days` does not exist). The
section alone is not a source: the file still needs a playlist. Bounds per night: at most `max_artists` artists,
`max_new` tracks and 20 minutes for the phase, inside the run's own `--max-new` / `--max-minutes`; a heavy playlist
night that uses them up leaves the artists `not_started` and they stay due. Per artist at most 5 source requests plus
1 search on the first check plus 1 `get_album` per missing release, at least 1 s between source calls; the source's
first refusal (HTTP 429/403) ends the phase without a retry. A YouTube sign-in challenge during a DOWNLOAD ends the
whole run with exit 3 as for any playlist track and is never bypassed; a bot-check text in the detector's own source
calls (search, get_artist, get_artist_albums, get_album) is classed as a refusal: it ends the phase with exit 1 (class
`partial`), not exit 3. An unexpected exception in the phase is logged by class only, ends the phase and gives exit 1.
With no stored ranking the phase does nothing and logs one line (there is no fallback to the artists in D1).

Partial releases (#238): an album row does not mean all its tracks landed. A release cut by a cap, failure or
interruption stays unevaluated and is fetched again when due; stored track IDs are skipped and missing tracks are
retried under the existing cooldown and caps. An already evaluated release is not repeatedly fetched. Matching
stored album titles remain eligible even outside the initial age filter, so an older partial release can resume.

Check state lives in `new-releases-state.json` next to `ingest-state.json` (hashed artist keys, dates, release keys).
A failed artist check, a failed track or an unreadable state file is a failure of the phase (exit 1, class `partial`),
the playlists are not affected. The shipped `ingest.log` gets counts and hashed keys only (`new releases lfm:<16 hex>:
...`, tracks are named by their `lfm:` song id): no artist or release names. The result JSON gets an additive
`new_releases` object (counts only: `due`, `checked`, `with_new`, `added`, `skipped`, `failed`, `not_started`, and a
`status` word); its track counts are also part of `counts`.

Quiet track failures and approved-cover failures include bounded context for recognized local error formats (#390):
for example, `IngestError [phase=telegram http_status=429 retry_after_seconds=25]`. Categories are fixed
(`telegram`, `d1`, `download`, `cover`, `tagging`); API status is a validated numeric code, and retry-after is shown
only for the exact recognized Telegram response format. Raw provider descriptions, URLs and names are not included;
unrecognized errors remain class-only. This adds diagnostic evidence without changing retries, cooldowns, caps or
sign-in stopping. A reported retry-after does not mean the operation was automatically retried or completed.

If a quiet track fails inside the downloader, ingest also keeps a **private** report next to its configured state
file, under `download-failures/`. It records the exact matched video ID, song ID, UTC time, error class/context and
up to 8192 characters of cleaned downloader detail. Known environment secrets, signed query strings and IPs are
redacted using the existing output sanitizer. The directory is 0700 and reports are 0600; at most ten reports
are retained, with the newest failure replacing an earlier report for the same song. Reports cannot be stored inside
the public checkout; symlinks and an insecure destination are refused. A failed diagnostic write logs only its error
class and leaves the original failure/cooldown unchanged. A successful download, later upload failure or sign-in
stop does not produce such a report. No report is shipped to Grafana or added to CI artifacts. Reports may still
contain private titles/URLs: inspect them locally and never paste or attach them publicly.

Investigation of #175 on 2026-10-17 resolved the one failed record from the manual run using eight paced source
requests and existing read-only D1 helpers. One isolated download with the service's PATH and existing downloader
flags then produced a 13,002,835-byte MP3, which was removed with its private scratch directory. No ingest state,
Telegram object or D1 row changed. The earlier stderr and exact earlier match were not retained, so this successful
reproduction does not establish the original error's cause. The private report fixes that evidence gap for future
failures; it does not change retry policy or imply a successful full ingest run.

## Album covers from YouTube Music (#248)

A track without a Spotify cover URL (Last.fm and new-release tracks) gets the album cover from the YouTube Music
search result the matcher chose: the result's thumbnail with the size suffix `=w1200-h1200-l90-rj`, uploaded through
the normal `sendPhoto` path. A cover is stored per album and kept forever, so the guard is strict. No cover is used when:
the track has no album or `Unknown Album`; the matched album name differs (similarity under 0.9, another number or
roman numeral, an edition word such as deluxe, remastered or live in the difference, or a plural-only difference
like `Lover` / `Lovers`); or the matched artist differs from the track's artist (bar 0.8, article-insensitive,
Latin/Cyrillic aware, plural-only differences refused). This artist check only gates the cover, never the audio match.
Known residual: two different albums whose names differ by less than the bar in some other way can still pass.

The download accepts only https from `lh3.googleusercontent.com` or `yt3.googleusercontent.com`, follows no redirect,
needs `Content-Type: image/jpeg` and the JPEG magic bytes, is capped at 10 MB and has a 60 s total deadline. A failed
cover is `WARN cover failed for <song id>` and never fails the track.

Log and summary (counts and ids only, never names):

- `cover skipped for <song id>: <class>` with class `no_album`, `no_match_album`, `album_mismatch` or `artist_mismatch`.
  Logged per track, not per album (several tracks of one album each log it). A match without a thumbnail is silent.
- The per-playlist and per-phase summary lines get `, N covers from the match` (counted after a successful upload) and
  `, N without a cover` (tracks the guard refused), each only when non-zero. The final `done:` line does not include these segments. They are not part of the result JSON.

## Artist photos from YouTube Music (#251)

A track without a Spotify URL (Last.fm and new-release tracks) can also give its artist a photo: one `get_artist(artists[0].id)` call per artist and run, using the artist id of the matched
search result; the thumbnail base plus `=w800-h800-p-l90-rj` (a square crop of the banner) is fetched through the same
hardened route as a cover (https, the two Google image hosts, no redirect, JPEG, 10 MB, 60 s) and uploaded with `sendPhoto`.
The file id is written to `artists.image_file_id` in the track's own D1 batch with
`COALESCE(artists.image_file_id, excluded.image_file_id)`, so a photo already stored in the row is kept by the SQL itself. Spotify tracks (any song with a Spotify URL, also one without a cover) never trigger it: a nightly run with
only playlists makes no extra call, no extra D1 query and no extra log line.

- Gate: the artist statement names `image_file_id` only when there is a photo to write, and before the first photo is
  considered the run checks `pragma_table_info('artists')` once. When the column is missing (migration 0003 not applied)
  the run logs `artist photos: off, ...` once and nothing else changes; track inserts cannot fail on the missing column.
  The stored photos are read in the same step (`SELECT id, image_file_id FROM artists WHERE image_file_id IS NOT NULL`),
  so an artist that already has a photo costs no call and its photo is not replaced.
- Guard: the id is the FIRST artist of the matched result, so only that artist's name is compared with the track's artist
  (bar 0.8, article-insensitive, Latin/Cyrillic aware, plural-only differences refused); a feature credit first and the
  track's artist second gives no photo (`artist photo skipped for <song id>: artist_mismatch`, the track is ingested as
  usual and the attempt is not remembered, so a later correct match of the artist can still get the photo). Compilation
  pseudo-artists (`Various Artists`, `Unknown Artist`, `VA`, ...) never get a photo (`pseudo_artist`). It gates only the
  photo, never the audio match. A match without an artist id is a silent no-op.
- Pacing and failure: 2 s pause before each `get_artist`, transient errors get at most 3 attempts in total (`--retry-attempts`), like the search. A
  failed photo is `WARN artist photo failed for <song id>: <error class>` and never fails the track; the artist is not
  retried in the same run. A sign-in challenge in that call ends the run with exit 3 as everywhere else (never bypassed); the track in progress is then discarded like any track in progress at exit 3 (its Telegram audio message is deleted and it is retried next run), and its already uploaded cover photo message stays orphaned in the channel.
- Log and summary: counts and ids only. The summary lines get `, N artist photos` when non-zero. Artists stored earlier
  without a photo are not filled here (the backfill is #253).

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
| `Last.fm error 8 in user.getWeeklyChartList` (or `user.getWeeklyArtistChart (week N of M)`, also from `lastfm_report.py`) | Last.fm answered "Operation failed" (its backend). The client already retried it 3 times with a 5/10/20 s backoff before giving up, so a lasting error 8 for the same method points at the account or the Last.fm side, not at your config (a bad key is error 10, a private profile error 17). Check: the profile page opens in a private browser window, the scrobbles are there, the API account status at last.fm/api/accounts. If only one week (N) fails, try again later |
| Track skipped as `no_match` or `too_big` | No acceptable YouTube Music match (title and duration within 3 s), or the mp3 exceeds Telegram's 20 MB limit (about 10 minutes at 256k) |

## Weekly metadata backup (#275)

`scripts/run-d1-backup.sh` exports D1 to a private local SQL file; it does not download audio. The owner installs the separate `cowlic-d1-backup.timer` (Sunday 20:00 local time, catch-up enabled), after the first real export and local restore rehearsal. Default retention: eight successful snapshots under the private state directory's `backups/`, 0700 directories and 0600 files. The existing narrow D1 token is loaded by Python from local `.env`; no secret is sourced by the shell or sent to Grafana. Logs contain byte/retention counts only and are available through `journalctl --user -u cowlic-d1-backup.service`.

The export briefly blocks database requests, so schedule it outside playback and ingest. Free-plan Time Travel is seven days; inspect backup failures or missed weeks and catch up manually. Full installation, exit codes, privacy limits, and the tested local restore procedure are in [worker-deploy-runbook.md section 12](worker-deploy-runbook.md#12-weekly-private-d1-export-275).

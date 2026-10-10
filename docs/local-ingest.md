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
- Observability is logs only. Nothing is sent anywhere on success or failure (decision #72): you read the log
  and `last-run.json`.

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

## Exit codes

| Code | From | Meaning | `failure_class` |
|---|---|---|---|
| 0 | ingest | Everything done; new tracks added, others already present | `none` |
| 1 | ingest | Some track or playlist failed, or sources/`.env` invalid | `partial` or `config` |
| 2 | ingest | Bad command-line usage (only when calling ingest directly) | none written |
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
| Track skipped as `no_match` or `too_big` | No acceptable YouTube Music match (title and duration within 3 s), or the mp3 exceeds Telegram's 20 MB limit (about 10 minutes at 256k) |

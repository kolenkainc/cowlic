#!/usr/bin/env bash
# Runs the Last.fm match-rate report (ingest/lastfm_report.py) with its output captured to a local log and shipped
# LIVE to Grafana Cloud Loki under its own job label (cowlic-report), logs only. Usage (arguments are forwarded to
# the report as they are):
#   scripts/run-lastfm-report.sh --lastfm-user <name> [--years 5] [--artists 100] ...
#
# - Never reads, sources or parses .env: the Python scripts load it themselves. Never traces, never prints env values.
# - The arguments are never echoed, logged or shipped. The value of --lastfm-user is handed to the redaction filter
#   and to the shipper through the environment (COWLIC_EXTRA_SECRET, never argv) and is removed from the local log
#   and from every shipped line.
# - Output (stdout and stderr of the report) -> ingest/ship_grafana.py --filter (redaction) -> tee -> the log
#   ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/lastfm-report.log (mode 0600, a fresh file per run; earlier ones kept as .1 ..).
# - While the report runs, a background loop ships the NEW complete lines every COWLIC_REPORT_SHIP_INTERVAL seconds
#   (default 20; bounded batch, offset kept in a file) and once more at the end. Shipping is best effort and never
#   changes the report's exit code. Logs only: no metrics, no last-run.json, nothing of the nightly status is touched.
# - Own flock lock (lastfm-report.lock): a second report logs one line to stderr and exits 75.
# - Exit code: the report's own; 128+N when interrupted by signal N. Wrapper failures: 78 (prerequisites), 75 (locked).
# - The background loop and the report run in their own process groups (job control) and are always stopped when
#   the wrapper ends or is interrupted (trap); after a kill -9 of the wrapper the loop ends itself within ~1 s.
# Test: scripts/test-run-lastfm-report.sh
set +x
set -uo pipefail
umask 077

if [ -z "${XDG_STATE_HOME:-}" ] && [ -z "${HOME:-}" ]; then
  echo "run-lastfm-report: neither XDG_STATE_HOME nor HOME is set; cannot place the log" >&2
  exit 78
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowlic"
LOG="$STATE_DIR/lastfm-report.log"
LOCK="$STATE_DIR/lastfm-report.lock"
OFFSET_FILE="$STATE_DIR/.lastfm-report.offset"
VENV_PY="$ROOT/ingest/.venv/bin/python"
REPORT="$ROOT/ingest/lastfm_report.py"
SHIP="$ROOT/ingest/ship_grafana.py"
JOB="cowlic-report"
LOG_KEEP="${COWLIC_LOG_KEEP:-5}"
EXIT_PREREQ=78
EXIT_LOCKED=75

# positive integers only, sane maxima (timeout 0 would mean no limit)
SHIP_TIMEOUT_S="${COWLIC_SHIP_TIMEOUT:-30}"
case "$SHIP_TIMEOUT_S" in '' | *[!0-9]* | 0*) SHIP_TIMEOUT_S=30 ;; esac
[ "${#SHIP_TIMEOUT_S}" -le 3 ] && [ "$SHIP_TIMEOUT_S" -le 120 ] || SHIP_TIMEOUT_S=30
INTERVAL="${COWLIC_REPORT_SHIP_INTERVAL:-20}"
case "$INTERVAL" in '' | *[!0-9]* | 0*) INTERVAL=20 ;; esac
[ "${#INTERVAL}" -le 4 ] && [ "$INTERVAL" -le 3600 ] || INTERVAL=20

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s run-lastfm-report: %s\n' "$(now_utc)" "$*" | tee -a "$LOG"; }

# The user name: only ever used as a redaction value. Both forms of the option; the last one wins like argparse.
LASTFM_USER=""
prev=""
for a in "$@"; do
  case "$a" in --lastfm-user=*) LASTFM_USER="${a#--lastfm-user=}" ;; esac
  [ "$prev" = --lastfm-user ] && LASTFM_USER="$a"
  prev="$a"
done
unset prev a

# A fresh log per run: a non-empty log of an earlier run always moves to .1 (older ones shift, LOG_KEEP kept), so
# this run's log starts empty and its first byte is a line start (an earlier run killed mid-line cannot matter).
rotate_log() {
  [ -s "$LOG" ] || return 0
  local i
  rm -f "$LOG.$LOG_KEEP"
  for ((i = LOG_KEEP - 1; i >= 1; i--)); do
    [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
  done
  mv -f "$LOG" "$LOG.1"
}

# One shipper call (logs only, job label cowlic-report, saved offset). Never changes anything: timeout, stdin
# closed, lock fd not inherited, stderr dropped, result ignored. The user name goes in through the environment.
ship_call() ( # own trap scope: timeout creates a separate group, so stop it explicitly
  set +m
  ship_pid=""
  trap 'if [ -n "$ship_pid" ]; then kill -TERM -- "-$ship_pid" 2> /dev/null || true; wait "$ship_pid" 2> /dev/null || true; fi' EXIT
  trap 'exit 143' TERM
  COWLIC_EXTRA_SECRET="$LASTFM_USER" timeout -k 2 "$SHIP_TIMEOUT_S" "$VENV_PY" "$SHIP" --logs-only --job "$JOB" \
    --log "$LOG" --offset "$LOG_OFFSET" --offset-file "$OFFSET_FILE" "$@" < /dev/null 9>&- 2> /dev/null &
  ship_pid=$!
  wait "$ship_pid"
)

WRAPPER_PID=$$
PIPE_PID=""
TICK_PID=""
WATCH_PID=""
INTERRUPTED=0
stop_group() { # pid: stop the process group led by this job
  [ -n "${1:-}" ] || return 0
  kill -TERM -- "-$1" 2> /dev/null
  wait "$1" 2> /dev/null
  return 0
}
cleanup() { trap - EXIT; stop_group "$WATCH_PID"; stop_group "$TICK_PID"; stop_group "$PIPE_PID"; rm -f "$OFFSET_FILE"; }
on_signal() { INTERRUPTED=$((128 + $1)); stop_group "$PIPE_PID"; }

if ! mkdir -p "$STATE_DIR" || ! chmod 700 "$STATE_DIR"; then
  echo "run-lastfm-report: cannot create state dir $STATE_DIR" >&2
  exit "$EXIT_PREREQ"
fi
cd "$ROOT" || { echo "run-lastfm-report: cannot cd to $ROOT" >&2; exit "$EXIT_PREREQ"; }
if ! command -v flock > /dev/null 2>&1; then
  echo "run-lastfm-report: flock not found; install util-linux" >&2
  exit "$EXIT_PREREQ"
fi
exec 9> "$LOCK"
if ! flock -n 9; then
  echo "$(now_utc) run-lastfm-report: another report is active (lock $LOCK); exiting without doing anything" >&2
  exit "$EXIT_LOCKED"
fi
if [ ! -x "$VENV_PY" ] || [ ! -f "$REPORT" ] || [ ! -f "$SHIP" ]; then
  echo "run-lastfm-report: ingest/.venv or the ingest scripts not found (see docs/local-ingest.md)" >&2
  exit "$EXIT_PREREQ"
fi

rotate_log
rm -f "$OFFSET_FILE"
LOG_OFFSET=0  # the log is fresh (see rotate_log)
trap cleanup EXIT
trap 'on_signal 2' INT
trap 'on_signal 15' TERM
trap 'on_signal 1' HUP
set -m  # every background job gets its own process group, so stop_group can end it with all its children
log "start (arguments are not logged)"

# The report: stdout+stderr -> redaction filter -> tee (terminal and log). The report's exit code comes from
# PIPESTATUS, so neither the filter nor tee can hide it. -u: unbuffered, so lines reach the log as they are printed.
{
  COWLIC_EXTRA_SECRET="$LASTFM_USER" "$VENV_PY" -u "$REPORT" "$@" 2>&1 < /dev/null 9>&- \
    | COWLIC_EXTRA_SECRET="$LASTFM_USER" "$VENV_PY" -u "$SHIP" --filter 9>&- \
    | tee -a "$LOG"
  exit "${PIPESTATUS[0]}"
} &
PIPE_PID=$!

# The live shipper: its own process group (stopped as a whole), output dropped.
# It exits by itself as soon as the wrapper is gone (kill -0 on its pid before every one-second sleep step and
# before every push), so even a kill -9 of the wrapper, which no trap survives, ends it within about a second.
(
  trap 'exit 0' TERM
  warned=0
  while :; do
    n=0
    while [ "$n" -lt "$INTERVAL" ]; do
      kill -0 "$WRAPPER_PID" 2> /dev/null || exit 0
      sleep 1
      n=$((n + 1))
    done
    kill -0 "$WRAPPER_PID" 2> /dev/null || exit 0
    out="$(ship_call)"
    case "$out" in
      *"secret found"*) # the final guard refused the body (e.g. a very short user name): say it once, on the terminal
        [ "$warned" = 1 ] || echo "run-lastfm-report: WARN grafana shipping refused by the secret guard; nothing is shipped" >&2
        warned=1 ;;
    esac
  done
) < /dev/null > /dev/null 9>&- &
TICK_PID=$!

# The ship_call trap stops its separately grouped timeout. A wrapper killed without traps still
# needs an independent observer to stop that group before it can update the offset after a final flush.
(
  while kill -0 "$WRAPPER_PID" 2> /dev/null; do sleep 1; done
  kill -TERM -- "-$TICK_PID" 2> /dev/null
) < /dev/null > /dev/null 2>&1 9>&- &
WATCH_PID=$!

wait "$PIPE_PID"
rc=$?
if [ "$INTERRUPTED" != 0 ]; then
  wait "$PIPE_PID" 2> /dev/null
  rc="$INTERRUPTED"
fi
stop_group "$WATCH_PID"
WATCH_PID=""
stop_group "$TICK_PID"
TICK_PID=""
PIPE_PID=""

log "finished: exit code $rc"
# Final push of whatever is left (also an unfinished last line). Only its first status line is shown, reduced to a
# harmless character set. Never changes the exit code.
out="$(ship_call --final)" || out=""
line="$(printf '%s\n' "$out" | head -n 1 | LC_ALL=C tr -cd 'A-Za-z0-9 .:,()_=*/-' | cut -c1-160)"
case "$line" in grafana:*) printf '%s run-lastfm-report: %s\n' "$(now_utc)" "$line" ;; esac
exit "$rc"

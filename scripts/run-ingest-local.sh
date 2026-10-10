#!/usr/bin/env bash
# One local ingest run: sources.yml -> Telegram + D1. Started daily by the systemd user timer
# (deploy/systemd/cowlic-ingest.*) or by hand. Logs only: no notification of any kind.
#
# - Never reads, sources or parses .env: ingest/ingest.py loads it itself (tg_check.load_env).
# - Never traces and never prints env values. Its own log lines carry no values; everything else in the
#   log is what ingest.py prints (it redacts its own output).
# - flock lock: a second run while one is active logs one line and exits without doing anything.
# - Log: ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/ingest.log, rotated by size (ingest.log.1 .. .N).
# - Exit code: the ingest exit code is passed through; any non-zero means the run failed (see
#   describe_ingest_exit). The wrapper's own failures use the codes in the block below.
# - After last-run.json: best-effort push of this run's redacted log to Grafana Cloud Loki (ship_grafana(); never
#   changes the exit code, see docs/local-ingest.md).
# - COWLIC_INGEST_ARGS (optional): allowlisted ingest caps (--max-new, --max-minutes, --pace-seconds), see parse_ingest_args.
# Test: scripts/test-run-ingest-local.sh
set +x
set -uo pipefail
umask 077

if [ -z "${XDG_STATE_HOME:-}" ] && [ -z "${HOME:-}" ]; then
  echo "run-ingest-local: neither XDG_STATE_HOME nor HOME is set; cannot place the log" >&2
  exit 78  # EXIT_PREREQ (defined below)
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowlic"
LOG="$STATE_DIR/ingest.log"
LOCK="$STATE_DIR/ingest.lock"
LAST_RUN="$STATE_DIR/last-run.json"
RESULT="$STATE_DIR/.ingest-result.json"  # written by ingest.py (--result-json), read into last-run.json, removed
VENV_PY="$ROOT/ingest/.venv/bin/python"
LOG_MAX_BYTES="${COWLIC_LOG_MAX_BYTES:-5242880}"  # rotate before a run when the log is at least this big
LOG_KEEP="${COWLIC_LOG_KEEP:-5}"                  # rotated files kept: ingest.log.1 .. ingest.log.$LOG_KEEP

# Exit codes of the wrapper itself. Ingest codes pass through unchanged (see describe_ingest_exit),
# so these stay clear of 0..64 and of 125..255 (shell and signal range). Ingest codes: 0 ok, 1 failed,
# 2 bad usage, 3 YouTube sign-in required.
EXIT_PREREQ=78  # venv, prerequisites check or state dir not usable; nothing ran
EXIT_LOCKED=75  # another run holds the lock; nothing ran

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s run-ingest-local: %s\n' "$(now_utc)" "$*" | tee -a "$LOG"; }

# Single place for the ingest exit-code mapping. Prints the failure class for the log line.
# Any non-zero code is a failure.
describe_ingest_exit() {
  case "$1" in
    0) echo "ok" ;;
    1) echo "failed" ;;
    3) echo "failed (youtube-sign-in)" ;;
    *) echo "failed (unmapped exit code)" ;;
  esac
}

# last-run.json: the status of the latest run, next to the log. Written after EVERY run that got past the lock
# (a lock-blocked invocation never touches it). Atomic (temp file in the same directory + rename), mode 0600.
# Fields: started_at, finished_at (UTC ISO 8601), exit_code, failure_class (none | partial | config |
# youtube-sign-in | crash), log_file (name only), result (ingest.py's --result-json object: playlists with
# status and counts added/skipped/no_match/too_big/failed/not_attempted, counts; or null when ingest.py did not
# produce one). Only counts, class names, timestamps and playlist URLs without query: no secret ever goes in.
# failure_class "crash": ingest.py ended with a non-zero code without writing its result (traceback, kill).
STARTED_AT=""
write_last_run() { # exit_code
  local rc="$1" class="none" result="null" tmp
  if [ -s "$RESULT" ]; then
    result="$(<"$RESULT")"
    # Must be one valid JSON object, checked with the venv python (bounded by timeout); without a usable
    # venv python or on any doubt the result is dropped (null), so last-run.json is always valid JSON.
    case "$result" in "{"*"}") ;; *) result="null" ;; esac
    if [ "$result" != null ]; then
      if [ -x "$VENV_PY" ] && timeout 10 "$VENV_PY" -I -c 'import json,sys; assert isinstance(json.load(sys.stdin), dict)' \
        < "$RESULT" > /dev/null 2>&1; then :; else result="null"; fi
    fi
    class="$(printf '%s' "$result" | sed -n 's/.*"failure_class": *"\([a-z-]*\)".*/\1/p')"
  fi
  case "$class" in none | partial | config | youtube-sign-in) ;; *) class="" ;; esac
  if [ "$rc" = 0 ]; then class="none"
  elif [ "$rc" = "$EXIT_PREREQ" ]; then class="config"
  elif [ -z "$class" ] || [ "$class" = none ]; then class="crash"
  fi
  tmp="$(mktemp "$STATE_DIR/.last-run.XXXXXX")" || { log "WARN could not write last-run.json"; return 0; }
  printf '{"started_at": "%s", "finished_at": "%s", "exit_code": %d, "failure_class": "%s", "log_file": "%s", "result": %s}\n' \
    "$STARTED_AT" "$(now_utc)" "$rc" "$class" "$(basename "$LOG")" "$result" > "$tmp" \
    && chmod 600 "$tmp" && mv -f "$tmp" "$LAST_RUN" \
    || { rm -f "$tmp"; log "WARN could not write last-run.json"; }
}

# Best-effort shipping of THIS run's redacted log lines (from byte LOG_OFFSET on) and a run_summary line to Grafana
# Cloud Loki (ingest/ship_grafana.py, which reads its credentials from .env itself). It can never change the run:
# runs after last-run.json is written, under timeout (killed after 30 s), stdin closed, lock fd not inherited, stderr
# dropped, result ignored. Only the first line of its stdout is logged, reduced to a harmless character set.
SHIP="$ROOT/ingest/ship_grafana.py"
SHIP_TIMEOUT_S="${COWLIC_SHIP_TIMEOUT:-30}"  # hard limit for the shipper (the test lowers it)
case "$SHIP_TIMEOUT_S" in '' | *[!0-9]* | 0* ) SHIP_TIMEOUT_S=30 ;; esac  # positive integer only: timeout 0 means no limit
[ "${#SHIP_TIMEOUT_S}" -le 3 ] && [ "$SHIP_TIMEOUT_S" -le 120 ] || SHIP_TIMEOUT_S=30  # sane maximum: 120 s
ship_grafana() {
  [ -x "$VENV_PY" ] && [ -f "$SHIP" ] || return 0
  local out ship_rc=0 line
  out="$(timeout -k 2 "$SHIP_TIMEOUT_S" "$VENV_PY" "$SHIP" --log "$LOG" --offset "$LOG_OFFSET" --last-run "$LAST_RUN" \
    --started-at "$STARTED_AT" < /dev/null 9>&- 2> /dev/null)" || ship_rc=$?
  if [ "$ship_rc" = 124 ] || [ "$ship_rc" = 137 ]; then
    log "WARN grafana shipping timed out"
  elif [ "$ship_rc" != 0 ]; then
    log "WARN grafana shipping failed"
  else
    line="$(printf '%s\n' "$out" | head -n 1 | LC_ALL=C tr -cd 'A-Za-z0-9 .:,()_=*/-' | cut -c1-160)"
    case "$line" in grafana:*) log "$line" ;; esac
  fi
  return 0
}

# Ends the run after the lock was taken: writes last-run.json, ships to Grafana (best effort), then exits with the
# code. The exit code is the argument, whatever shipping does.
finish() { local rc="$1"; write_last_run "$rc"; rm -f "$RESULT"; ship_grafana || true; exit "$rc"; }

rotate_log() {
  [ -f "$LOG" ] || return 0
  local size i
  size="$(wc -c < "$LOG")"
  ((size >= LOG_MAX_BYTES)) || return 0
  rm -f "$LOG.$LOG_KEEP"
  for ((i = LOG_KEEP - 1; i >= 1; i--)); do
    [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
  done
  mv -f "$LOG" "$LOG.1"
}

if ! mkdir -p "$STATE_DIR" || ! chmod 700 "$STATE_DIR"; then
  echo "run-ingest-local: cannot create state dir $STATE_DIR" >&2
  exit "$EXIT_PREREQ"
fi

cd "$ROOT" || { echo "run-ingest-local: cannot cd to $ROOT" >&2; exit "$EXIT_PREREQ"; }

if ! command -v flock >/dev/null 2>&1; then
  log "ERROR flock not found; install util-linux (see scripts/check-local-prereqs.sh)"
  exit "$EXIT_PREREQ"
fi
exec 9>"$LOCK"
if ! flock -n 9; then
  # stderr only: the running run owns the log and last-run.json, a blocked invocation touches neither
  echo "$(now_utc) run-ingest-local: another run is active (lock $LOCK); exiting without doing anything" >&2
  exit "$EXIT_LOCKED"
fi

STARTED_AT="$(now_utc)"
rm -f "$RESULT"  # no stale result of an earlier run can be mistaken for this one
rotate_log
# Where this run's lines begin in the log (after the rotation above; rotation happens only here, under the lock).
LOG_OFFSET="$({ wc -c < "$LOG"; } 2> /dev/null)" || LOG_OFFSET=0
case "$LOG_OFFSET" in '' | *[!0-9]*) LOG_OFFSET=0 ;; esac
log "start"

# COWLIC_INGEST_ARGS (optional): extra ingest options, e.g. "--max-new 200 --max-minutes 90". Never eval'd, never
# expanded by the shell: the value must first be made only of letters, digits, spaces and '-', is then split into
# words with read -a (no globbing), and every word must be an allowlisted option followed by an integer in its range.
# Anything else ends the run with EXIT_PREREQ before ingest starts. The log line never contains the rejected value.
EXTRA_ARGS=()
parse_ingest_args() {
  local raw="${COWLIC_INGEST_ARGS-}" words=() i opt val max seen=" "
  case "$raw" in *[![:space:]]*) ;; *) return 0 ;; esac  # unset, empty or blank: today's behaviour
  if [ "${#raw}" -gt 200 ]; then log "ERROR COWLIC_INGEST_ARGS rejected: too long"; return 1; fi
  case "$raw" in *[!A-Za-z0-9\ -]*) log "ERROR COWLIC_INGEST_ARGS rejected: only letters, digits, spaces and '-' are allowed"; return 1 ;; esac
  read -r -a words <<<"$raw"
  for ((i = 0; i < ${#words[@]}; i += 2)); do
    opt="${words[i]}"; val="${words[i + 1]-}"
    case "$opt" in
      --max-new) max=1000000 ;;
      --max-minutes) max=1440 ;;
      --pace-seconds) max=3600 ;;
      *) log "ERROR COWLIC_INGEST_ARGS rejected: unknown option (allowed: --max-new --max-minutes --pace-seconds)"; return 1 ;;
    esac
    case "$seen" in *" $opt "*) log "ERROR COWLIC_INGEST_ARGS rejected: $opt given twice"; return 1 ;; esac
    seen="$seen$opt "
    case "$val" in
      '' | *[!0-9]* | 0*) log "ERROR COWLIC_INGEST_ARGS rejected: $opt needs a positive integer"; return 1 ;;
    esac
    if [ "${#val}" -gt 7 ] || [ "$val" -gt "$max" ]; then
      log "ERROR COWLIC_INGEST_ARGS rejected: $opt out of range (1..$max)"; return 1
    fi
    EXTRA_ARGS+=("$opt" "$val")
  done
  log "extra ingest options from COWLIC_INGEST_ARGS:${seen% } (values validated)"
}
parse_ingest_args || finish "$EXIT_PREREQ"

if [ ! -x "$VENV_PY" ]; then
  log "ERROR ingest/.venv not found. Run: python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt"
  finish "$EXIT_PREREQ"
fi

PREREQ="$ROOT/scripts/check-local-prereqs.sh"
if [ -x "$PREREQ" ]; then
  # deno is optional here: ingest.py runs without it and only logs a warning, so a deno problem is a WARN line.
  prereq_out="$("$PREREQ" --deno-optional 2>&1)"
  prereq_rc=$?
  if ((prereq_rc != 0)); then
    log "ERROR prerequisites check failed:"
    printf '%s\n' "$prereq_out" | tee -a "$LOG"
    finish "$EXIT_PREREQ"
  fi
  # Log the warnings only (the "ok" lines are noise on every run).
  while IFS= read -r line; do
    case "$line" in WARN*) log "prerequisites: $line" ;; esac
  done <<<"$prereq_out"
fi

# Pipe through tee for the log; the ingest exit code comes from PIPESTATUS, so tee cannot hide it.
# -u: unbuffered, so a run killed by systemd's timeout still leaves its latest output in the log.
# 9>&-: the lock descriptor is not inherited by ingest and its children.
"$VENV_PY" -u "$ROOT/ingest/ingest.py" --sources "$ROOT/sources.yml" --result-json "$RESULT" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} 9>&- 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}

log "finished: exit code $rc, $(describe_ingest_exit "$rc")"
finish "$rc"

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
    case "$result" in "{"*"}") ;; *) result="null" ;; esac
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

# Ends the run after the lock was taken: writes last-run.json, then exits with the code.
finish() { write_last_run "$1"; rm -f "$RESULT"; exit "$1"; }

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
log "start"

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
"$VENV_PY" -u "$ROOT/ingest/ingest.py" --sources "$ROOT/sources.yml" --result-json "$RESULT" 9>&- 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}

log "finished: exit code $rc, $(describe_ingest_exit "$rc")"
finish "$rc"

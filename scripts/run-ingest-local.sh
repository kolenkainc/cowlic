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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowlic"
LOG="$STATE_DIR/ingest.log"
LOCK="$STATE_DIR/ingest.lock"
VENV_PY="$ROOT/ingest/.venv/bin/python"
LOG_MAX_BYTES="${COWLIC_LOG_MAX_BYTES:-5242880}"  # rotate before a run when the log is at least this big
LOG_KEEP="${COWLIC_LOG_KEEP:-5}"                  # rotated files kept: ingest.log.1 .. ingest.log.$LOG_KEEP

# Exit codes of the wrapper itself. Ingest codes pass through unchanged (see describe_ingest_exit),
# so these stay clear of 0..64 and of 125..255 (shell and signal range). #66 adds an ingest code for the
# YouTube sign-in failure and #31 writes last-run.json: both hook in at describe_ingest_exit.
EXIT_PREREQ=78  # venv, prerequisites check or state dir not usable; nothing ran
EXIT_LOCKED=75  # another run holds the lock; nothing ran

log() { printf '%s run-ingest-local: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

# Single place for the ingest exit-code mapping. Prints the failure class for the log line.
# Any non-zero code is a failure. To follow #66 (sign-in code) and #31 (last-run.json), add cases here.
describe_ingest_exit() {
  case "$1" in
    0) echo "ok" ;;
    1) echo "failed" ;;
    *) echo "failed (unmapped exit code)" ;;
  esac
}

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
  log "another run is active (lock $LOCK); exiting without doing anything"
  exit "$EXIT_LOCKED"
fi

rotate_log
log "start"

if [ ! -x "$VENV_PY" ]; then
  log "ERROR ingest/.venv not found. Run: python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt"
  exit "$EXIT_PREREQ"
fi

PREREQ="$ROOT/scripts/check-local-prereqs.sh"
if [ -x "$PREREQ" ]; then
  prereq_out="$("$PREREQ" 2>&1)"
  prereq_rc=$?
  if ((prereq_rc != 0)); then
    log "ERROR prerequisites check failed:"
    printf '%s\n' "$prereq_out" | tee -a "$LOG"
    exit "$EXIT_PREREQ"
  fi
fi

# Pipe through tee for the log; the ingest exit code comes from PIPESTATUS, so tee cannot hide it.
"$VENV_PY" "$ROOT/ingest/ingest.py" --sources "$ROOT/sources.yml" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}

log "finished: exit code $rc, $(describe_ingest_exit "$rc")"
exit "$rc"

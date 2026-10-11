#!/usr/bin/env bash
# One local bootstrap command: ingest/bootstrap.py (rank | plan | run | status), started by hand or by the coordinator,
# NOT by the 21:10 timer. Design: docs/bootstrap-ongoing.md (sections 2.1, 2.2, 5). Usage (arguments are forwarded to
# bootstrap.py as they are, never echoed):
#   scripts/run-bootstrap-local.sh run  --max-new N --max-minutes M [--config FILE] ...
#   scripts/run-bootstrap-local.sh plan --max-new N --max-minutes M [--config FILE]
#   scripts/run-bootstrap-local.sh rank [--config FILE] [--refresh]
#   scripts/run-bootstrap-local.sh status
#
# - Never reads, sources or parses .env: bootstrap.py loads it itself. Never traces (set +x), never prints env values,
#   no eval: the arguments are only matched by `case`, then forwarded verbatim as an array.
# - Argument rules (checked first, nothing is touched on a violation: one stderr line, exit 78): the subcommand is one of
#   rank|plan|run|status; run and plan REQUIRE --max-new (1..1000000) and --max-minutes (1..1440), each once, as
#   "--opt N" or "--opt=N"; rank and status take no --max-*; any abbreviation of --max-* or any prefix of --result-json from --r on is rejected
#   (argparse would accept it behind the wrapper's back); --result-json belongs to the wrapper (it is added for run).
# - Lock: the SAME file as the nightly wrapper (ingest.lock), taken exactly like it: exec 9>, flock -n 9, exit 75 with
#   one stderr line before anything else; a blocked invocation touches neither the log nor bootstrap-last-run.json.
#   The child is started with 9>&- (the descriptor is not inherited) and with COWLIC_LOCK_HELD=1 ON ITS COMMAND LINE
#   ONLY, which tells bootstrap.py "the wrapper holds the lock, do not take it again". The variable is never exported,
#   never in the unit or in .env; an inherited COWLIC_LOCK_HELD is unset here, so no other child of this wrapper sees it.
#   Reading order for bootstrap.py (#168): COWLIC_LOCK_HELD from the real process environment BEFORE any .env load;
#   a value in .env is ignored.
# - Overlap guard (run and plan): refuses to start (exit 78, one stderr line, nothing touched) when now + --max-minutes
#   + 10 minutes margin + 60 s (the timer's RandomizedDelaySec) reaches the next 21:10 LOCAL time. The next 21:10 is
#   computed from the clock (the timer's OnCalendar is a fixed local time), not read from systemctl. Within 60 s after
#   21:10 the run may still be about to start, so that day's 21:10 counts as next. Test hook:
#   COWLIC_BOOTSTRAP_NOW_EPOCH (epoch seconds) replaces the clock.
# - Output (stdout and stderr of bootstrap.py) -> ingest/ship_grafana.py --filter (redaction of LASTFM_API_KEY, the
#   Grafana token and COWLIC_EXTRA_SECRET: put the Last.fm user name there if it must never reach the log) -> tee ->
#   ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/bootstrap.log (0600, rotated by size like ingest.log: .1 .. .N).
# - bootstrap-last-run.json next to it: removed at the start of a session (after the lock and the guard), so a killed
#   wrapper leaves none; written after EVERY invocation that got past the lock and the guard (started_at,
#   finished_at, subcommand, exit_code, stop_reason, log_file, result = bootstrap.py's --result-json object or null).
#   Always valid JSON, atomic, 0600. NEVER touches last-run.json or ingest.log and never calls ship_grafana.py for
#   shipping (only its --filter mode): no Grafana push, the owner is informed through the log only.
# - Exit code: bootstrap.py's own (0 ok, 1 failed, 2 usage, 3 YouTube sign-in stop); 128+N when interrupted by signal N.
#   Wrapper: 75 locked, 78 prerequisite/usage/guard.
# - The child and its helpers run in their own process group; they are stopped when the wrapper ends or gets a signal.
#   After kill -9 of the wrapper a watchdog (which keeps the lock descriptor) ends the group within ~1 s, then the
#   lock is released: no session keeps running without the lock.
# Test: scripts/test-run-bootstrap-local.sh
set +x
set -uo pipefail
umask 077
unset COWLIC_LOCK_HELD  # see the lock note above: only the child's command line carries it

EXIT_PREREQ=78  # usage, guard, venv, prerequisites or state dir problem; nothing ran
EXIT_LOCKED=75  # another run or session holds the lock; nothing ran

# ---- arguments (pure checks, no side effects) --------------------------------------------------------------------
usage_fail() { echo "run-bootstrap-local: $* (usage: run-bootstrap-local.sh rank|plan|run|status; run and plan need --max-new 1..1000000 and --max-minutes 1..1440)" >&2; exit "$EXIT_PREREQ"; }

SUB="${1-}"
[ "$#" -eq 0 ] || shift
case "$SUB" in rank | plan | run | status) ;; *) usage_fail "missing or unknown subcommand" ;; esac
ARGS=("$@")
MAX_NEW=""
MAX_MINUTES=""
check_cap() { # option value max: sets the global named by the option, rejects a second one
  local opt="$1" val="$2" max="$3"
  case "$val" in '' | *[!0-9]* | 0*) usage_fail "$opt needs a positive integer" ;; esac
  if [ "${#val}" -gt 7 ] || [ "$val" -gt "$max" ]; then usage_fail "$opt out of range (1..$max)"; fi
  case "$opt" in
    --max-new) [ -z "$MAX_NEW" ] || usage_fail "--max-new given twice"; MAX_NEW="$val" ;;
    --max-minutes) [ -z "$MAX_MINUTES" ] || usage_fail "--max-minutes given twice"; MAX_MINUTES="$val" ;;
  esac
}
n_args="${#ARGS[@]}"
i=0
while [ "$i" -lt "$n_args" ]; do
  a="${ARGS[i]}"
  case "$a" in
    --r*)
      name="${a%%=*}"  # every prefix of --result-json (at least --r) is an abbreviation argparse could accept
      case "--result-json" in "$name"*) usage_fail "--result-json is set by the wrapper" ;; esac ;;
    --m*)
      case "$SUB" in rank | status) usage_fail "$SUB takes no --max-* option" ;; esac
      case "$a" in
        --max-new | --max-minutes)
          i=$((i + 1))
          [ "$i" -lt "$n_args" ] || usage_fail "$a needs a value"
          if [ "$a" = --max-new ]; then check_cap "$a" "${ARGS[i]}" 1000000; else check_cap "$a" "${ARGS[i]}" 1440; fi ;;
        --max-new=*) check_cap --max-new "${a#--max-new=}" 1000000 ;;
        --max-minutes=*) check_cap --max-minutes "${a#--max-minutes=}" 1440 ;;
        *) usage_fail "unknown or abbreviated --m* option" ;;
      esac ;;
  esac
  i=$((i + 1))
done
case "$SUB" in
  run | plan) { [ -n "$MAX_NEW" ] && [ -n "$MAX_MINUTES" ]; } || usage_fail "$SUB requires --max-new and --max-minutes" ;;
esac
unset i a n_args name

# ---- paths -------------------------------------------------------------------------------------------------------
if [ -z "${XDG_STATE_HOME:-}" ] && [ -z "${HOME:-}" ]; then
  echo "run-bootstrap-local: neither XDG_STATE_HOME nor HOME is set; cannot place the log" >&2
  exit "$EXIT_PREREQ"
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowlic"
LOG="$STATE_DIR/bootstrap.log"
LOCK="$STATE_DIR/ingest.lock"  # the nightly wrapper's lock: one lock for both processes
LAST_RUN="$STATE_DIR/bootstrap-last-run.json"
RESULT="$STATE_DIR/.bootstrap-result.json"  # written by bootstrap.py run (--result-json), read into last-run, removed
VENV_PY="$ROOT/ingest/.venv/bin/python"
BOOTSTRAP="$ROOT/ingest/bootstrap.py"
SHIP="$ROOT/ingest/ship_grafana.py"  # only its --filter mode is used
LOG_MAX_BYTES="${COWLIC_LOG_MAX_BYTES:-5242880}"
LOG_KEEP="${COWLIC_LOG_KEEP:-5}"
case "$LOG_MAX_BYTES" in '' | *[!0-9]*) LOG_MAX_BYTES=5242880 ;; esac
case "$LOG_KEEP" in '' | *[!0-9]* | 0*) LOG_KEEP=5 ;; esac

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s run-bootstrap-local: %s\n' "$(now_utc)" "$*" | tee -a "$LOG"; }

describe_exit() {
  case "$1" in
    0) echo "ok" ;;
    1) echo "failed" ;;
    2) echo "failed (usage or configuration)" ;;
    3) echo "failed (youtube-sign-in)" ;;
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

# bootstrap-last-run.json (see the header). stop_reason: the top-level "stop_reason" string of the result object when it
# is there (letters and '-' only), else "sign-in" for exit 3, else "".
STARTED_AT=""
write_last_run() { # exit_code
  local rc="$1" result="null" stop="" tmp
  if [ -s "$RESULT" ]; then
    result="$(<"$RESULT")"
    case "$result" in "{"*"}") ;; *) result="null" ;; esac
    if [ "$result" != null ]; then
      if [ -x "$VENV_PY" ] && timeout 10 "$VENV_PY" -I -c 'import json,sys; assert isinstance(json.load(sys.stdin), dict)' \
        < "$RESULT" > /dev/null 2>&1; then
        stop="$(timeout 10 "$VENV_PY" -I -c 'import json,sys; v=json.load(sys.stdin).get("stop_reason"); print(v if isinstance(v,str) else "")' \
          < "$RESULT" 2> /dev/null | head -n 1 | LC_ALL=C tr -cd 'a-z-' | cut -c1-20)" || stop=""
      else
        result="null"
      fi
    fi
  fi
  if [ -z "$stop" ] && [ "$rc" = 3 ]; then stop="sign-in"; fi
  tmp="$(mktemp "$STATE_DIR/.bootstrap-last-run.XXXXXX")" || { log "WARN could not write bootstrap-last-run.json"; return 0; }
  printf '{"started_at": "%s", "finished_at": "%s", "subcommand": "%s", "exit_code": %d, "stop_reason": "%s", "log_file": "%s", "result": %s}\n' \
    "$STARTED_AT" "$(now_utc)" "$SUB" "$rc" "$stop" "$(basename "$LOG")" "$result" > "$tmp" \
    && chmod 600 "$tmp" && mv -f "$tmp" "$LAST_RUN" \
    || { rm -f "$tmp"; log "WARN could not write bootstrap-last-run.json"; }
}

PIPE_PID=""
WATCH_PID=""
INTERRUPTED=0
stop_group() { # pid: stop the process group led by this job
  [ -n "${1:-}" ] || return 0
  kill -TERM -- "-$1" 2> /dev/null
  wait "$1" 2> /dev/null
  return 0
}
cleanup() { trap - EXIT; stop_group "$WATCH_PID"; stop_group "$PIPE_PID"; rm -f "$RESULT"; }
on_signal() { INTERRUPTED=$((128 + $1)); stop_group "$PIPE_PID"; }
finish() { local rc="$1"; write_last_run "$rc"; rm -f "$RESULT"; exit "$rc"; }

# ---- state dir and lock ------------------------------------------------------------------------------------------
if ! mkdir -p "$STATE_DIR" || ! chmod 700 "$STATE_DIR"; then
  echo "run-bootstrap-local: cannot create state dir $STATE_DIR" >&2
  exit "$EXIT_PREREQ"
fi
cd "$ROOT" || { echo "run-bootstrap-local: cannot cd to $ROOT" >&2; exit "$EXIT_PREREQ"; }
if ! command -v flock > /dev/null 2>&1; then
  echo "run-bootstrap-local: flock not found; install util-linux (see scripts/check-local-prereqs.sh)" >&2
  exit "$EXIT_PREREQ"
fi
exec 9> "$LOCK"
if ! flock -n 9; then
  echo "$(now_utc) run-bootstrap-local: another run or session is active (lock $LOCK); exiting without doing anything" >&2
  exit "$EXIT_LOCKED"
fi

# ---- 21:10 overlap guard (run and plan) --------------------------------------------------------------------------
guard_fail() { echo "run-bootstrap-local: $*" >&2; exit "$EXIT_PREREQ"; }
case "$SUB" in
  run | plan)
    NOW="${COWLIC_BOOTSTRAP_NOW_EPOCH-}"
    if [ -n "$NOW" ]; then
      case "$NOW" in *[!0-9]* | 0*) guard_fail "COWLIC_BOOTSTRAP_NOW_EPOCH must be epoch seconds" ;; esac
      [ "${#NOW}" -le 11 ] || guard_fail "COWLIC_BOOTSTRAP_NOW_EPOCH must be epoch seconds"
    else
      NOW="$(date +%s)"
    fi
    DAY="$(date -d "@$NOW" +%F)" || guard_fail "cannot compute the local date"
    NEXT="$(date -d "$DAY 21:10:00" +%s)" || guard_fail "cannot compute the next 21:10"
    if [ "$NOW" -gt $((NEXT + 60)) ]; then  # within 60 s after 21:10 the timer's run may still be about to start
      NEXT="$(date -d "$DAY 21:10:00 +1 day" +%s)" || guard_fail "cannot compute the next 21:10"
    fi
    if [ $((NOW + MAX_MINUTES * 60 + 600 + 60)) -ge "$NEXT" ]; then
      FIT=$(((NEXT - NOW - 661) / 60))
      [ "$FIT" -ge 0 ] || FIT=0
      guard_fail "refusing to start: now + --max-minutes $MAX_MINUTES + 10 min margin reaches the next 21:10 local time (at most $FIT minutes fit now)"
    fi
    ;;
esac

# ---- start -------------------------------------------------------------------------------------------------------
STARTED_AT="$(now_utc)"
rm -f "$RESULT"
rm -f "$LAST_RUN"  # a kill -9 (no trap runs) must not leave the previous run's record looking like this session's
rotate_log
log "start: $SUB (arguments are not logged; --max-new ${MAX_NEW:-none}, --max-minutes ${MAX_MINUTES:-none})"

if [ ! -x "$VENV_PY" ] || [ ! -f "$BOOTSTRAP" ] || [ ! -f "$SHIP" ]; then
  log "ERROR ingest/.venv or ingest/bootstrap.py or ingest/ship_grafana.py not found. Run: python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt"
  finish "$EXIT_PREREQ"
fi

PREREQ="$ROOT/scripts/check-local-prereqs.sh"
if [ -x "$PREREQ" ]; then
  prereq_out="$("$PREREQ" --deno-optional 2>&1 9>&-)"
  prereq_rc=$?
  if ((prereq_rc != 0)); then
    log "ERROR prerequisites check failed:"
    printf '%s\n' "$prereq_out" | tee -a "$LOG"
    finish "$EXIT_PREREQ"
  fi
  while IFS= read -r line; do
    case "$line" in WARN*) log "prerequisites: $line" ;; esac
  done <<<"$prereq_out"
fi

trap cleanup EXIT
trap 'on_signal 2' INT
trap 'on_signal 15' TERM
trap 'on_signal 1' HUP
set -m  # every background job gets its own process group, so stop_group can end it with all its children

EXTRA=()
[ "$SUB" != run ] || EXTRA=(--result-json "$RESULT")

# The session: stdout+stderr -> redaction filter -> tee (terminal and log). Its exit code comes from PIPESTATUS, so
# neither the filter nor tee can hide it. -u: unbuffered. 9>&-: nobody in the group holds the lock; COWLIC_LOCK_HELD=1
# is on the child's command line only. stdin closed.
{
  COWLIC_LOCK_HELD=1 "$VENV_PY" -u "$BOOTSTRAP" "$SUB" ${ARGS[@]+"${ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"} 2>&1 < /dev/null 9>&- \
    | "$VENV_PY" -u "$SHIP" --filter 9>&- \
    | tee -a "$LOG"
  exit "${PIPESTATUS[0]}"
} 9>&- &
PIPE_PID=$!

# Watchdog: it keeps the lock descriptor on purpose. When the wrapper is gone (kill -9: no trap runs) it stops the group
# (TERM, KILL after 10 s) and only then lets the lock go. It is stopped by cleanup on every normal end.
WRAPPER_PID=$$
(
  while kill -0 "$WRAPPER_PID" 2> /dev/null; do sleep 1; done
  kill -TERM -- "-$PIPE_PID" 2> /dev/null
  n=0
  while kill -0 -- "-$PIPE_PID" 2> /dev/null && [ "$n" -lt 10 ]; do sleep 1; n=$((n + 1)); done
  kill -KILL -- "-$PIPE_PID" 2> /dev/null
  exit 0
) < /dev/null > /dev/null 2>&1 &
WATCH_PID=$!

wait "$PIPE_PID"
rc=$?
if [ "$INTERRUPTED" != 0 ]; then
  wait "$PIPE_PID" 2> /dev/null
  rc="$INTERRUPTED"
fi
stop_group "$WATCH_PID"
WATCH_PID=""
PIPE_PID=""

log "finished: exit code $rc, $(describe_exit "$rc")"
finish "$rc"

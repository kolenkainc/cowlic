#!/usr/bin/env bash
# Scheduled bootstrap launcher (#279): started by cowlic-bootstrap.service every 2 hours (deploy/systemd/), it sizes ONE
# bootstrap session so that it ends before the next 21:10 nightly run and starts it through scripts/run-bootstrap-local.sh
# run. No arguments. The wrapper keeps every control (lock shared with the 21:10 run, the 21:10 guard, redaction, last-run
# file, Grafana log shipping); this launcher only chooses the caps and decides whether to start at all.
#
# - Caps: --max-minutes = the largest value that passes the wrapper guard (now + minutes + 10 min margin + 60 s < next
#   21:10 local time), at most 100. Fewer than 15 minutes fit: skip (exit 0, one line). --max-new = 4 per minute, at most
#   400. Test hook (same as the wrapper): COWLIC_BOOTSTRAP_NOW_EPOCH replaces the clock.
# - Circuit breaker (state dir = ${XDG_STATE_HOME:-$HOME/.local/state}/cowlic, files mode 0600):
#     bootstrap-halt   written after a session that ended with exit 3 (YouTube sign-in challenge, rule #62: the bot check is
#                      never bypassed and a session is never retried), or after COWLIC_BOOTSTRAP_MAX_FAILS (default 3)
#                      consecutive sessions that failed and added no track. While it exists every scheduled run exits 0
#                      with "halted ...". Only the owner removes it, after reading the log: rm <state dir>/bootstrap-halt
#     bootstrap-done   written when a session ends ok with stop reason "done" (every ranked artist done, parked or excluded).
#                      Later runs exit 0 quietly. Remove it to resume (for example after a ranking refresh).
#     bootstrap-fails  the consecutive-failure counter (a number).
#   Exit 75 (the lock is held by the 21:10 run or a hand-started session) is not a failure: quiet skip, counters unchanged.
#   Exit 78 from the wrapper counts as a skip only when the time no longer fits (re-checked with a fresh clock); any
#   other 78 (prerequisites, usage) is a failure like the rest.
# - Exit code: 0 for a session that ran, was skipped or is halted/done; 1 when this run tripped the breaker (so the unit
#   shows failed once and the owner looks); 78 for a problem of the launcher itself (nothing ran).
# - Output goes to stdout (journal); the wrapper's own output is passed through. Nothing here reads .env, prints
#   environment values or arguments, or traces.
# Test: scripts/test-run-bootstrap-scheduled.sh
set +x
set -uo pipefail
umask 077

MIN_FIT_MINUTES=15
MAX_MINUTES_CAP=100
NEW_PER_MINUTE=4
MAX_NEW_CAP=400
EXIT_PREREQ=78
EXIT_LOCKED=75
EXIT_SIGN_IN=3

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$ROOT/scripts/run-bootstrap-local.sh"
if [ -z "${XDG_STATE_HOME:-}" ] && [ -z "${HOME:-}" ]; then
  echo "run-bootstrap-scheduled: neither XDG_STATE_HOME nor HOME is set" >&2
  exit "$EXIT_PREREQ"
fi
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cowlic"
HALT="$STATE_DIR/bootstrap-halt"
DONE="$STATE_DIR/bootstrap-done"
FAILS="$STATE_DIR/bootstrap-fails"
LAST_RUN="$STATE_DIR/bootstrap-last-run.json"

MAX_FAILS="${COWLIC_BOOTSTRAP_MAX_FAILS:-3}"
case "$MAX_FAILS" in '' | *[!0-9]* | 0*) MAX_FAILS=3 ;; esac
[ "${#MAX_FAILS}" -le 2 ] || MAX_FAILS=3

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s run-bootstrap-scheduled: %s\n' "$(now_utc)" "$*"; }

if [ ! -x "$WRAPPER" ]; then
  echo "run-bootstrap-scheduled: $WRAPPER not found or not executable" >&2
  exit "$EXIT_PREREQ"
fi
if ! mkdir -p "$STATE_DIR" || ! chmod 700 "$STATE_DIR"; then
  echo "run-bootstrap-scheduled: cannot create state dir $STATE_DIR" >&2
  exit "$EXIT_PREREQ"
fi

# Markers first: a halted or finished bootstrap does nothing, quietly.
if [ -e "$HALT" ]; then
  log "halted (marker $HALT: $(head -c 200 "$HALT" 2> /dev/null | LC_ALL=C tr -cd 'A-Za-z0-9 .:,_=/-')); not starting. Read the log, then remove the marker to resume"
  exit 0
fi
if [ -e "$DONE" ]; then
  log "bootstrap is done (marker $DONE); nothing to do. Disable the timer: systemctl --user disable --now cowlic-bootstrap.timer"
  exit 0
fi

# fit_minutes: prints the largest --max-minutes the wrapper guard accepts now (can be negative or 0). Same arithmetic as
# the wrapper: now + minutes*60 + 600 + 60 < next 21:10 local; within 60 s after 21:10 that day's 21:10 still counts.
fit_minutes() {
  local now="${COWLIC_BOOTSTRAP_NOW_EPOCH-}" day next
  if [ -n "$now" ]; then
    case "$now" in *[!0-9]* | 0*) return 1 ;; esac
    [ "${#now}" -le 11 ] || return 1
  else
    now="$(date +%s)" || return 1
  fi
  day="$(date -d "@$now" +%F)" || return 1
  next="$(date -d "$day 21:10:00" +%s)" || return 1
  if [ "$now" -gt $((next + 60)) ]; then
    next="$(date -d "$day 21:10:00 +1 day" +%s)" || return 1
  fi
  echo $(((next - now - 661) / 60))
}

FIT="$(fit_minutes)" || { echo "run-bootstrap-scheduled: cannot compute the time before the next 21:10 (bad COWLIC_BOOTSTRAP_NOW_EPOCH?)" >&2; exit "$EXIT_PREREQ"; }
if [ "$FIT" -lt "$MIN_FIT_MINUTES" ]; then
  log "skip: only $FIT minute(s) fit before the next 21:10 run (minimum $MIN_FIT_MINUTES)"
  exit 0
fi
MINUTES="$FIT"
[ "$MINUTES" -le "$MAX_MINUTES_CAP" ] || MINUTES="$MAX_MINUTES_CAP"
NEW=$((MINUTES * NEW_PER_MINUTE))
[ "$NEW" -le "$MAX_NEW_CAP" ] || NEW="$MAX_NEW_CAP"

write_marker() { # file text: atomic, mode 0600 (umask)
  local tmp
  tmp="$(mktemp "$STATE_DIR/.bootstrap-marker.XXXXXX")" || { log "WARN could not write $1"; return 1; }
  printf '%s\n' "$2" > "$tmp" && mv -f "$tmp" "$1" || { rm -f "$tmp"; log "WARN could not write $1"; return 1; }
}

read_fails() {
  local n=0
  [ -f "$FAILS" ] && n="$(head -c 6 "$FAILS" 2> /dev/null | LC_ALL=C tr -cd '0-9')"
  [ -n "$n" ] || n=0
  echo $((10#$n))
}

# last-run fields of the session that just ended (the wrapper removed the old file at its start): "<stop_reason> <added>"
last_run_fields() {
  local out=""
  [ -s "$LAST_RUN" ] && out="$(timeout 10 python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
r = d.get("result") if isinstance(d, dict) else None
r = r if isinstance(r, dict) else {}
c = r.get("counts") if isinstance(r.get("counts"), dict) else {}
s = r.get("stop_reason") if isinstance(r.get("stop_reason"), str) else "-"
a = c.get("added") if isinstance(c.get("added"), int) else 0
print(s, a)
' "$LAST_RUN" 2> /dev/null | head -n 1 | LC_ALL=C tr -cd 'a-z0-9 -')"
  case "$out" in *" "*) echo "$out" ;; *) echo "- 0" ;; esac
}

log "starting a session: --max-minutes $MINUTES --max-new $NEW"
"$WRAPPER" run --max-new "$NEW" --max-minutes "$MINUTES"
rc=$?

case "$rc" in
  "$EXIT_LOCKED")
    log "skip: another run or session holds the lock"
    exit 0 ;;
  "$EXIT_SIGN_IN")
    write_marker "$HALT" "$(now_utc) exit 3: YouTube sign-in challenge" || true
    log "ERROR halted after a YouTube sign-in challenge (exit 3). No retry, no workaround (rule #62). Read the log, wait, then remove $HALT"
    exit 1 ;;
  "$EXIT_PREREQ")
    now_fit="$(fit_minutes)" || now_fit=""
    if [ -n "$now_fit" ] && [ "$now_fit" -lt "$MINUTES" ]; then
      log "skip: the session no longer fits before the next 21:10 run"
      exit 0
    fi ;;
esac

read -r STOP ADDED < <(last_run_fields)
if [ "$rc" = 0 ]; then
  rm -f "$FAILS"
  if [ "$STOP" = done ]; then
    write_marker "$DONE" "$(now_utc) every ranked artist is done, parked or excluded" || true
    log "bootstrap is done: every ranked artist is done, parked or excluded. Disable the timer: systemctl --user disable --now cowlic-bootstrap.timer"
  fi
  exit 0
fi

# the session failed (exit 1, 2, 78 for another reason, a signal): only "failed and added nothing" counts
if [ "$ADDED" -gt 0 ]; then
  rm -f "$FAILS"
  log "session exit code $rc, but $ADDED track(s) were added: not counted as a failed session"
  exit 0
fi
n=$(($(read_fails) + 1))
write_marker "$FAILS" "$n" || true
log "session failed (exit code $rc) and added no track: $n of $MAX_FAILS consecutive"
if [ "$n" -ge "$MAX_FAILS" ]; then
  write_marker "$HALT" "$(now_utc) $n consecutive failed sessions without a new track" || true
  log "ERROR halted after $n consecutive failed sessions without a new track. Read the log, then remove $HALT"
  exit 1
fi
exit 0

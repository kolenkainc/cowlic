#!/usr/bin/env bash
# Test for run-bootstrap-local.sh on a fake repo copy in a temp dir with a STUB bootstrap (a stub venv python plays
# ingest/bootstrap.py, the redaction filter and the prerequisites script). Never runs the real bootstrap, never touches the
# network. Last section: mutation checks (each safety rule is broken in a copy of the wrapper and the test must notice).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failures=0
assert() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi; }
has() { grep -q -- "$1" "$2" 2> /dev/null && echo 0 || echo 1; }
hasnt() { grep -q -- "$1" "$2" 2> /dev/null && echo 1 || echo 0; }
is() { [ "$1" = "$2" ] && echo 0 || echo 1; }

export TZ=UTC  # the guard works on local time; UTC makes the epochs below exact
epoch() { date -u -d "$1" +%s; }
NOON="$(epoch '2026-10-11 12:00:00')"
export COWLIC_BOOTSTRAP_NOW_EPOCH="$NOON"  # test clock; guard cases override it
USERNAME_SECRET="sentinel_lfm_user"

WRAPPER_SRC="$ROOT/scripts/run-bootstrap-local.sh"
new_case() { # NAME: fake repo with the wrapper, a stub prereqs script, a stub venv python and a state dir
  C="$TMP/$1"; R="$C/root"; S="$C/state"
  mkdir -p "$R/scripts" "$R/ingest/.venv/bin" "$S"
  cp "$WRAPPER_SRC" "$R/scripts/run-bootstrap-local.sh"
  cat > "$R/scripts/check-local-prereqs.sh" <<'SH'
#!/bin/sh
echo "$@" > "$STUB_PREREQ_ARGS"
echo "lock-held=${COWLIC_LOCK_HELD-unset}" > "$STUB_PREREQ_ARGS.env"
[ -z "${STUB_PREREQ_FAIL:-}" ] || { echo "MISSING  stub: fix it"; exit 1; }
[ -z "${STUB_DENO_MISSING:-}" ] || echo "WARN     deno: not on PATH"
echo "All prerequisites present."
SH
  cat > "$R/ingest/.venv/bin/python" <<'PY'
#!/bin/sh
[ "${1:-}" != -I ] || exec python3 "$@"  # the wrapper validates the result JSON with `python -I -c ...`
case "${1:-}" in */ship_grafana.py) set -- -u "$@" ;; esac  # the shipping calls come without -u: same position as the filter
case "${2:-}" in
  */ship_grafana.py)
    case " $* " in
      *" --filter "*)  # the redaction filter
        echo "$*" >> "$STUB_FILTER_ARGS"
        echo "lock-held=${COWLIC_LOCK_HELD-unset}" >> "$STUB_FILTER_ARGS.env"
        [ ! -e /proc/self/fd/9 ] || echo leaked >> "$STUB_FILTER_ARGS.fd9"
        if [ -n "${COWLIC_EXTRA_SECRET:-}" ]; then exec sed -u "s/$COWLIC_EXTRA_SECRET/***/g"; else exec cat; fi ;;
    esac
    # the stub SHIPPER (logs-only calls): records its arguments; a --final call also copies what a real push would
    # carry (the log from --offset on) into STUB_SHIPPED, so the test can look at the shipped bytes
    echo "$*" >> "$STUB_SHIP_CALLS"
    echo "lock-held=${COWLIC_LOCK_HELD-unset}" >> "$STUB_SHIP_CALLS.env"
    [ ! -e /proc/self/fd/9 ] || echo leaked >> "$STUB_SHIP_CALLS.fd9"
    case " $* " in
      *" --final "*)
        off=""; lg=""; prev=""
        for a in "$@"; do
          [ "$prev" = --offset ] && off="$a"
          [ "$prev" = --log ] && lg="$a"
          prev="$a"
        done
        tail -c +"$((off + 1))" "$lg" >> "$STUB_SHIPPED" ;;
    esac
    case "${STUB_SHIP:-ok}" in
      ok) echo "grafana: loki push ok (HTTP 204)"; exit 0 ;;
      fail) echo "grafana: boom"; exit 1 ;;
      kill) kill -9 $$ ;;
      hang) sleep 12; exit 0 ;;
    esac ;;
  */bootstrap.py)
    shift 2  # -u and the script
    printf '%s\n' "$@" > "$STUB_ARGS"  # one argument per line
    echo "lock-held=${COWLIC_LOCK_HELD-unset}" > "$STUB_ARGS.env"
    [ ! -e /proc/self/fd/9 ] || echo leaked > "$STUB_ARGS.fd9"
    echo "stub line for user $COWLIC_EXTRA_SECRET"
    echo "stub error on stderr" >&2
    prev=""
    for a in "$@"; do
      [ "$prev" = --result-json ] && [ -n "${STUB_RESULT:-}" ] && printf '%s\n' "$STUB_RESULT" > "$a"
      prev="$a"
    done
    [ -z "${STUB_SLEEP:-}" ] || exec sleep "$STUB_SLEEP"
    exit "${STUB_RC:-0}" ;;
esac
PY
  chmod +x "$R/scripts/"*.sh "$R/ingest/.venv/bin/python"
  : > "$R/ingest/bootstrap.py"; : > "$R/ingest/ship_grafana.py"  # the wrapper only checks that they exist
  export STUB_ARGS="$C/args" STUB_FILTER_ARGS="$C/filter_args" STUB_PREREQ_ARGS="$C/prereq_args"
  export STUB_SHIP_CALLS="$C/ship_calls" STUB_SHIPPED="$C/shipped"
}
run() { rc=0; XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" "$@" > "$C/out" 2>&1 || rc=$?; }
LOGF() { echo "$S/cowlic/bootstrap.log"; }
nothing_started() { [ ! -e "$STUB_ARGS" ] && [ ! -e "$S/cowlic/bootstrap.log" ] && [ ! -e "$S/cowlic/bootstrap-last-run.json" ] && echo 0 || echo 1; }

# 1. success path: forwarding, lock handoff, redaction, own files, nightly files untouched, logs-only Grafana shipping
new_case ok
mkdir -p "$S/cowlic"
printf 'nightly log line\n' > "$S/cowlic/ingest.log"; printf '{"nightly": true}\n' > "$S/cowlic/last-run.json"
nightly_before="$(cksum "$S/cowlic/ingest.log" "$S/cowlic/last-run.json")"
STUB_RESULT='{"added": 4, "stop_reason": "cap"}'; STUB_RC=0; export STUB_RESULT STUB_RC
COWLIC_EXTRA_SECRET="$USERNAME_SECRET" COWLIC_LOCK_HELD=0 run run --max-new 5 --max-minutes=10 --config 'a b;$(touch PWNED)' --unpark
assert "run: exit 0" "$(is "$rc" 0)"
assert "args are forwarded verbatim, in order, then --result-json" "$([ "$(cat "$STUB_ARGS")" = "$(printf '%s\n' run --max-new 5 --max-minutes=10 --config 'a b;$(touch PWNED)' --unpark --result-json "$S/cowlic/.bootstrap-result.json")" ] && echo 0 || echo 1)"
assert "nothing in the arguments is executed" "$([ ! -e "$R/PWNED" ] && [ ! -e PWNED ] && echo 0 || echo 1)"
assert "child sees COWLIC_LOCK_HELD=1 (even if the caller had 0)" "$(has '^lock-held=1$' "$STUB_ARGS.env")"
assert "child does not hold the lock fd 9" "$([ ! -e "$STUB_ARGS.fd9" ] && echo 0 || echo 1)"
assert "the filter does not see COWLIC_LOCK_HELD" "$(has '^lock-held=unset$' "$STUB_FILTER_ARGS.env")"
assert "the prerequisites script does not see COWLIC_LOCK_HELD" "$(has '^lock-held=unset$' "$STUB_PREREQ_ARGS.env")"
assert "the filter does not hold fd 9" "$([ ! -e "$STUB_FILTER_ARGS.fd9" ] && echo 0 || echo 1)"
assert "the filter is only ever called with --filter" "$([ "$(grep -vc -- '--filter' "$STUB_FILTER_ARGS" || true)" = 0 ] && echo 0 || echo 1)"
assert "prereqs called with --deno-optional" "$(has '--deno-optional' "$STUB_PREREQ_ARGS")"
assert "log has the output (stdout and stderr)" "$(has 'stub error on stderr' "$(LOGF)")"
assert "user name redacted in the log" "$(hasnt "$USERNAME_SECRET" "$(LOGF)")"
assert "redacted line present with the marker" "$(has 'stub line for user \*\*\*' "$(LOGF)")"
assert "user name not on the terminal" "$(hasnt "$USERNAME_SECRET" "$C/out")"
assert "log has start and finished lines" "$(has 'finished: exit code 0, ok' "$(LOGF)")"
assert "the arguments are not logged" "$(hasnt 'PWNED\|--unpark\|--config' "$(LOGF)")"
assert "log mode 0600, state dir 0700" "$([ "$(stat -c %a "$(LOGF)")" = 600 ] && [ "$(stat -c %a "$S/cowlic")" = 700 ] && echo 0 || echo 1)"
LR="$S/cowlic/bootstrap-last-run.json"
assert "bootstrap-last-run.json is valid JSON with the fields" "$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
assert d["exit_code"]==0 and d["subcommand"]=="run" and d["stop_reason"]=="cap" and d["result"]=={"added":4,"stop_reason":"cap"}
assert d["started_at"].endswith("Z") and d["finished_at"].endswith("Z") and d["log_file"]=="bootstrap.log"
' "$LR" > /dev/null 2>&1 && echo 0 || echo 1)"
assert "last-run file is mode 0600" "$([ "$(stat -c %a "$LR")" = 600 ] && echo 0 || echo 1)"
assert "nightly ingest.log and last-run.json untouched" "$([ "$(cksum "$S/cowlic/ingest.log" "$S/cowlic/last-run.json")" = "$nightly_before" ] && echo 0 || echo 1)"
assert "result temp file removed" "$([ ! -e "$S/cowlic/.bootstrap-result.json" ] && echo 0 || echo 1)"
unset STUB_RESULT STUB_RC

# 2. the other subcommands: no --result-json, no caps for rank/status
new_case rank; run rank --refresh --config x
assert "rank: ok and no --result-json" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS")" = "$(printf '%s\n' rank --refresh --config x)" ] && echo 0 || echo 1)"
new_case status; run status
assert "status: ok" "$(is "$rc" 0)"
new_case plan; run plan --max-minutes 30 --max-new 7
assert "plan: caps in any order, no --result-json" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS")" = "$(printf '%s\n' plan --max-minutes 30 --max-new 7)" ] && echo 0 || echo 1)"

# 3. exit codes pass through; the sign-in stop is named; a crash without a result leaves result null
for code in 1 2 3 42; do
  new_case "rc$code"; STUB_RC=$code; export STUB_RC; run run --max-new 1 --max-minutes 1
  assert "exit code $code passed through" "$(is "$rc" "$code")"
  assert "code $code recorded in last-run and the log" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["exit_code"]==int(sys.argv[2]) and d["result"] is None' "$S/cowlic/bootstrap-last-run.json" "$code" > /dev/null 2>&1 && echo 0 || echo 1)"
done
unset STUB_RC
new_case rc3b; STUB_RC=3; export STUB_RC; run run --max-new 1 --max-minutes 1; unset STUB_RC
assert "exit 3: stop_reason sign-in" "$(python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["stop_reason"]=="sign-in"' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
assert "exit 3: failed (youtube-sign-in) in the log" "$(has 'failed (youtube-sign-in)' "$(LOGF)")"
new_case badres; STUB_RESULT='{"a": ' ; export STUB_RESULT; run run --max-new 1 --max-minutes 1; unset STUB_RESULT
assert "invalid result JSON: last-run still valid JSON, result null" "$(python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["result"] is None' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"

# 4. argument rules: exit 78 before anything starts (no child, no log, no status file)
reject() { # description, args...
  local d="$1"; shift
  new_case "rej-$RANDOM"; run "$@"
  assert "rejected, exit 78, nothing started: $d" "$([ "$rc" = 78 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"
}
reject "no subcommand"
reject "unknown subcommand" bogus --max-new 1 --max-minutes 1
reject "run without caps" run
reject "run without --max-minutes" run --max-new 5
reject "run without --max-new" run --max-minutes 5
reject "plan without caps" plan
reject "--max-new 0" run --max-new 0 --max-minutes 5
reject "--max-new 1000001" run --max-new 1000001 --max-minutes 5
reject "--max-minutes 1441" run --max-new 5 --max-minutes 1441
reject "--max-minutes non-numeric" run --max-new 5 --max-minutes 5x
reject "--max-new empty" run --max-new= --max-minutes 5
reject "--max-new missing value" run --max-minutes 5 --max-new
reject "--max-new twice" run --max-new 1 --max-new 2 --max-minutes 5
reject "abbreviated --max-n" run --max-n 5 --max-minutes 5
reject "abbreviation after valid caps" run --max-new 5 --max-minutes 5 --max-m 999999
reject "rank with --max-new" rank --max-new 5
reject "status with --max-minutes=5" status --max-minutes=5
reject "status with abbreviated --max" status --max 5
reject "--result-json supplied by the caller" run --max-new 1 --max-minutes 1 --result-json /tmp/x
reject "abbreviated --result-j" run --max-new 1 --max-minutes 1 --resu /tmp/x
for ab in --r --re --res --resul --result-js --result-json=/tmp/x; do reject "prefix $ab of --result-json" run --max-new 1 --max-minutes 1 "$ab" /tmp/x; done
new_case ok-reset; run run --max-new 1 --max-minutes 1 --reset-state --refresh
assert "--reset-state and --refresh are not mistaken for --result-json" "$(is "$rc" 0)"
new_case maxok; run run --max-new 1000000 --max-minutes 1440
assert "maxima 1000000 and 1440 are only refused by the guard, not by the range check" "$(has 'reaches the next 21:10' "$C/out")"
new_case rej-out; run run --max-new 1 --max-minutes bogus-value-123
assert "the rejected value is not echoed" "$(hasnt 'bogus-value-123' "$C/out")"

# 5. 21:10 guard (UTC): now + --max-minutes + 10 min margin + 60 s must stay below the next 21:10
guard_case() { # description, now, max-minutes, expected rc
  new_case "g-$RANDOM"; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch "$2")" run run --max-new 1 --max-minutes "$3"
  assert "guard: $1" "$(is "$rc" "$4")"
  if [ "$4" = 78 ]; then assert "guard: refusal is one stderr line and touches nothing ($1)" "$([ "$(wc -l < "$C/out")" = 1 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"; fi
}
guard_case "12:00, 60 min: ends well before 21:10" '2026-10-11 12:00:00' 60 0
guard_case "12:00, 538 min: ends 20:58 + margin 21:09, fits" '2026-10-11 12:00:00' 538 0
guard_case "12:00, 539 min: 20:59 + 11 min reaches 21:10, refused" '2026-10-11 12:00:00' 539 78
guard_case "12:00, 1440 min crosses 21:10" '2026-10-11 12:00:00' 1440 78
guard_case "21:09:30: any run is refused" '2026-10-11 21:09:30' 1 78
guard_case "21:10:30: the timer may still start, refused" '2026-10-11 21:10:30' 1 78
guard_case "21:11:30 (just after): next day, 60 min fits" '2026-10-11 21:11:30' 60 0
guard_case "21:11:30 (just after): 1440 min crosses tomorrow 21:10" '2026-10-11 21:11:30' 1440 78
guard_case "23:55: next day, 600 min fits" '2026-10-11 23:55:00' 600 0
guard_case "00:05 after midnight: today's 21:10 is next, 1260 min crosses" '2026-10-12 00:05:00' 1260 78
new_case g-msg; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 12:00:00')" run plan --max-new 1 --max-minutes 600
assert "guard applies to plan too and names how many minutes fit" "$([ "$rc" = 78 ] && [ "$(has 'at most 538 minutes fit now' "$C/out")" = 0 ] && echo 0 || echo 1)"
new_case g-rank; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 21:09:30')" run rank
assert "rank and status are not subject to the guard" "$(is "$rc" 0)"
new_case g-bad; COWLIC_BOOTSTRAP_NOW_EPOCH="12ab" run run --max-new 1 --max-minutes 1
assert "invalid test clock: 78, nothing started" "$([ "$rc" = 78 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"
new_case g-real; env -u COWLIC_BOOTSTRAP_NOW_EPOCH XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" plan --max-new 1 --max-minutes 1440 > "$C/out" 2>&1 || rc=$?
assert "real clock: 1440 minutes always crosses the next 21:10" "$([ "$rc" = 78 ] && [ "$(has 'reaches the next 21:10' "$C/out")" = 0 ] && echo 0 || echo 1)"

# 6. lock: the nightly wrapper's file; a held lock gives 75 and touches nothing; released afterwards
new_case lock; mkdir -p "$S/cowlic"
( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 4 ) 9> /dev/null &
holder=$!
sleep 0.5
COWLIC_LOCK_HELD=1 run run --max-new 1 --max-minutes 1
assert "held lock: exit 75 (even with COWLIC_LOCK_HELD=1 in the caller's environment)" "$(is "$rc" 75)"
assert "held lock: one line, nothing started, nothing touched" "$([ "$(has 'another run or session is active' "$C/out")" = 0 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"
run status
assert "held lock: status gets 75 as well" "$(is "$rc" 75)"
kill "$holder" 2> /dev/null || true; wait "$holder" 2> /dev/null || true
run run --max-new 1 --max-minutes 1
assert "lock released: runs again" "$(is "$rc" 0)"
# the nightly wrapper's lock name is the one used
assert "same lock file name as the nightly wrapper" "$(has 'LOCK=.*ingest.lock' "$ROOT/scripts/run-ingest-local.sh")"
assert "wrapper uses ingest.lock" "$(has 'LOCK="\$STATE_DIR/ingest.lock"' "$WRAPPER_SRC")"
# while a session runs, a second invocation gets 75 and the first is unaffected
new_case lock2; STUB_SLEEP=3; export STUB_SLEEP
( XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out1" 2>&1 ) &
bg=$!
for _ in $(seq 50); do [ -s "$STUB_ARGS" ] && break; sleep 0.1; done
rm -f "$STUB_ARGS"
run run --max-new 1 --max-minutes 1
assert "second session while one runs: 75, no child" "$([ "$rc" = 75 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1)"
wait "$bg" && assert "first session unaffected" 0 || assert "first session unaffected" 1
unset STUB_SLEEP

# 7. prerequisites: venv, bootstrap.py, failing prerequisites -> 78 and a status file; deno only warns
new_case novenv; rm -f "$R/ingest/.venv/bin/python"; run status
assert "no venv: exit 78, hint logged, status file says 78" "$([ "$rc" = 78 ] && [ "$(has 'ingest/.venv or ingest/bootstrap.py' "$(LOGF)")" = 0 ] && python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["exit_code"]==78' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
new_case nobs; rm -f "$R/ingest/bootstrap.py"; run status
assert "no bootstrap.py: exit 78" "$(is "$rc" 78)"
new_case prereq; STUB_PREREQ_FAIL=1; export STUB_PREREQ_FAIL; run status; unset STUB_PREREQ_FAIL
assert "prerequisites failure: 78, child not started" "$([ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && [ "$(has 'prerequisites check failed' "$(LOGF)")" = 0 ] && echo 0 || echo 1)"
new_case deno; STUB_DENO_MISSING=1; export STUB_DENO_MISSING; run status; unset STUB_DENO_MISSING
assert "deno missing: still runs, warning in the log" "$([ "$rc" = 0 ] && [ "$(has 'prerequisites: WARN     deno' "$(LOGF)")" = 0 ] && echo 0 || echo 1)"

# 8. log rotation by size, like the nightly log
new_case rot; mkdir -p "$S/cowlic"; head -c 300 /dev/zero | tr '\0' 'x' > "$S/cowlic/bootstrap.log"
COWLIC_LOG_MAX_BYTES=100 run status
assert "log over the limit rotates to .1" "$([ -s "$S/cowlic/bootstrap.log.1" ] && [ "$(has 'xxxxxxxx' "$(LOGF)")" = 1 ] && echo 0 || echo 1)"

# 9. bash -x leaks nothing (secrets only ever live in the environment)
new_case xtrace
rc=0; COWLIC_EXTRA_SECRET="$USERNAME_SECRET" XDG_STATE_HOME="$S" bash -x "$R/scripts/run-bootstrap-local.sh" status > "$C/out" 2>&1 || rc=$?
assert "bash -x: success and no user name in the output or the log" "$([ "$rc" = 0 ] && [ "$(hasnt "$USERNAME_SECRET" "$C/out")" = 0 ] && [ "$(hasnt "$USERNAME_SECRET" "$(LOGF)")" = 0 ] && echo 0 || echo 1)"

# 10. SIGTERM: child stopped, no stray process, 143 recorded; kill -9: the watchdog ends the group, then the lock is free
sleeper_case() { # NAME: the stub child is a long sleep with a unique marker
  new_case "$1"; MARK="31337.$RANDOM$RANDOM"
  cat > "$R/ingest/.venv/bin/python" <<PY
#!/bin/sh
[ "\${1:-}" != -I ] || exec python3 "\$@"
case "\${2:-}" in
  */ship_grafana.py) exec cat ;;
  */bootstrap.py) echo started; exec sleep $MARK ;;
esac
PY
}
sleeper_case term
XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out" 2>&1 &
wp=$!
sleep 2
kill -TERM "$wp"
rc=0; wait "$wp" || rc=$?
sleep 1
assert "SIGTERM: exit code 143" "$(is "$rc" 143)"
assert "SIGTERM: no stray process" "$([ -z "$(pgrep -f -- "sleep $MARK|$R/ingest/.venv/bin/python|$R/scripts/run-bootstrap-local.sh" || true)" ] && echo 0 || echo 1)"
assert "SIGTERM: status file records 143" "$(python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["exit_code"]==143' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
assert "SIGTERM: lock released" "$(flock -n "$S/cowlic/ingest.lock" true && echo 0 || echo 1)"

sleeper_case kill9
mkdir -p "$S/cowlic"; echo '{"stale": true}' > "$S/cowlic/bootstrap-last-run.json"
XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out" 2>&1 &
wp=$!
sleep 2
kill -9 "$wp"
wait "$wp" 2> /dev/null || true
sleep 4
assert "SIGKILL: the watchdog ended the session, no stray process" "$([ -z "$(pgrep -f -- "sleep $MARK|$R/ingest/.venv/bin/python|$R/scripts/run-bootstrap-local.sh" || true)" ] && echo 0 || echo 1)"
assert "SIGKILL: the previous bootstrap-last-run.json is gone, not mistaken for this session" "$([ ! -e "$S/cowlic/bootstrap-last-run.json" ] && echo 0 || echo 1)"
assert "SIGKILL: lock released afterwards" "$(flock -n "$S/cowlic/ingest.lock" true && echo 0 || echo 1)"
pkill -f -- "sleep $MARK" 2> /dev/null || true

# 10b. Grafana shipping (#244): logs only, job cowlic-bootstrap, live, best effort; locked or refused runs ship nothing
CANARY_KEY="SENTINEL_LASTFM_API_KEY_123"; CANARY_TOKEN="SENTINEL_GRAFANA_TOKEN_456"
new_case ship; mkdir -p "$S/cowlic"; printf 'OLD SESSION LINE\n' > "$S/cowlic/bootstrap.log"
printf '{"nightly": true}\n' > "$S/cowlic/last-run.json"; printf 'nightly\n' > "$S/cowlic/ingest.log"
STUB_RC=1; export STUB_RC
COWLIC_EXTRA_SECRET="$USERNAME_SECRET" run run --max-new 3 --max-minutes 7
unset STUB_RC
assert "ship: exit code of the session is untouched" "$(is "$rc" 1)"
assert "ship: every shipper call is logs-only with job cowlic-bootstrap" "$([ -s "$STUB_SHIP_CALLS" ] && [ "$(grep -vc -e '--logs-only --job cowlic-bootstrap ' "$STUB_SHIP_CALLS" || true)" = 0 ] && echo 0 || echo 1)"
assert "ship: the calls carry an offset file and the last one is the final flush" "$([ "$(tail -n 1 "$STUB_SHIP_CALLS" | grep -c -e '--final')" = 1 ] && grep -q -e '--offset-file' "$STUB_SHIP_CALLS" && echo 0 || echo 1)"
assert "ship: no metrics: no --last-run, --started-at or nightly job label in any call" "$(hasnt '--last-run\|--started-at\|cowlic-ingest\|PROM' "$STUB_SHIP_CALLS")"
assert "ship: no secret or user name in the shipper's arguments" "$(hasnt "$USERNAME_SECRET\|$CANARY_KEY\|$CANARY_TOKEN" "$STUB_SHIP_CALLS")"
assert "ship: the shipper does not hold the lock fd and does not see COWLIC_LOCK_HELD" "$([ ! -e "$STUB_SHIP_CALLS.fd9" ] && [ "$(grep -vc '^lock-held=unset$' "$STUB_SHIP_CALLS.env" || true)" = 0 ] && echo 0 || echo 1)"
assert "ship: the shipped bytes start at this session (earlier log content is not shipped)" "$([ "$(has 'start: run' "$STUB_SHIPPED")" = 0 ] && [ "$(hasnt 'OLD SESSION LINE' "$STUB_SHIPPED")" = 0 ] && echo 0 || echo 1)"
assert "ship: the shipped bytes hold the session output and the finished line" "$([ "$(has 'stub error on stderr' "$STUB_SHIPPED")" = 0 ] && [ "$(has 'finished: exit code 1' "$STUB_SHIPPED")" = 0 ] && echo 0 || echo 1)"
assert "ship: the user name is not in the shipped bytes (redacted before the log)" "$(hasnt "$USERNAME_SECRET" "$STUB_SHIPPED")"
assert "ship: the offset file is gone at the end" "$([ ! -e "$S/cowlic/.bootstrap-ship.offset" ] && echo 0 || echo 1)"
assert "ship: nightly ingest.log and last-run.json untouched" "$([ "$(cat "$S/cowlic/ingest.log")" = nightly ] && [ "$(cat "$S/cowlic/last-run.json")" = '{"nightly": true}' ] && echo 0 || echo 1)"
assert "ship: bootstrap-last-run.json has the same fields as before (exit_code 1, result null)" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["exit_code"]==1 and d["result"] is None and set(d)=={"started_at","finished_at","subcommand","exit_code","stop_reason","log_file","result"}' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
assert "ship: the shipper's status line is logged" "$(has 'grafana: loki push ok' "$(LOGF)")"

new_case ship-plan; run plan --max-new 1 --max-minutes 1
assert "ship: plan ships as well" "$([ "$rc" = 0 ] && grep -q -e '--logs-only --job cowlic-bootstrap' "$STUB_SHIP_CALLS" && echo 0 || echo 1)"
new_case ship-status; run status; run rank
assert "ship: status and rank ship nothing" "$([ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1)"

for mode in fail kill; do
  new_case "ship-$mode"; STUB_SHIP="$mode"; STUB_RC=3; export STUB_SHIP STUB_RC; run run --max-new 1 --max-minutes 1; unset STUB_SHIP STUB_RC
  assert "ship $mode: exit code 3 unchanged and recorded" "$([ "$rc" = 3 ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["exit_code"]==3 and d["stop_reason"]=="sign-in"' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
done
new_case ship-hang; STUB_SHIP=hang STUB_RC=5 COWLIC_SHIP_TIMEOUT=1 run run --max-new 1 --max-minutes 1
assert "ship hang: cut by the timeout, exit code 5 unchanged" "$(is "$rc" 5)"
new_case ship-ok0; run run --max-new 1 --max-minutes 1
assert "ship: a clean session exits 0 and records 0" "$([ "$rc" = 0 ] && python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["exit_code"]==0' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"

new_case ship-live; STUB_SLEEP=4 COWLIC_BOOTSTRAP_SHIP_INTERVAL=1 run run --max-new 1 --max-minutes 1
assert "ship live: ticks (with the offset file, without --final) while the session runs, one final flush" "$([ "$(grep -vc -e '--final' "$STUB_SHIP_CALLS" || true)" -ge 2 ] && [ "$(grep -c -e '--final' "$STUB_SHIP_CALLS")" = 1 ] && echo 0 || echo 1)"

new_case ship-interval; COWLIC_BOOTSTRAP_SHIP_INTERVAL=0 run run --max-new 1 --max-minutes 1
assert "ship: an invalid interval falls back to the default (no tight loop)" "$([ "$rc" = 0 ] && [ "$(wc -l < "$STUB_SHIP_CALLS")" -le 2 ] && echo 0 || echo 1)"

new_case ship-torn; mkdir -p "$S/cowlic"; printf 'torn line without newline' > "$S/cowlic/bootstrap.log"
run run --max-new 1 --max-minutes 1
assert "ship: a log that ends mid-line gets its line closed, the shipped part starts at a line start" "$([ "$(has 'start: run' "$STUB_SHIPPED")" = 0 ] && [ "$(hasnt 'torn line' "$STUB_SHIPPED")" = 0 ] && echo 0 || echo 1)"

# nothing ships for refused, locked or failed-before-start invocations
new_case ship-usage; run run
assert "ship: usage error ships nothing" "$([ "$rc" = 78 ] && [ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1)"
new_case ship-guard; run run --max-new 1 --max-minutes 600
assert "ship: guard refusal ships nothing" "$([ "$rc" = 78 ] && [ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1)"
new_case ship-lock; mkdir -p "$S/cowlic"
( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 3 ) 9> /dev/null &
holder=$!; sleep 0.4
run run --max-new 1 --max-minutes 1
kill "$holder" 2> /dev/null || true; wait "$holder" 2> /dev/null || true
assert "ship: locked invocation (75) ships nothing and creates no offset file" "$([ "$rc" = 75 ] && [ ! -e "$STUB_SHIP_CALLS" ] && [ ! -e "$S/cowlic/.bootstrap-ship.offset" ] && echo 0 || echo 1)"
new_case ship-prereq; STUB_PREREQ_FAIL=1; export STUB_PREREQ_FAIL; run run --max-new 1 --max-minutes 1; unset STUB_PREREQ_FAIL
assert "ship: failed prerequisites (78, session never started) ship nothing" "$([ "$rc" = 78 ] && [ ! -e "$STUB_SHIP_CALLS" ] && [ ! -e "$S/cowlic/.bootstrap-ship.offset" ] && echo 0 || echo 1)"

# SIGTERM during a shipping session: 143, one final flush, no stray process (loop included), lock released
new_case ship-term
STUB_SLEEP=30 COWLIC_BOOTSTRAP_SHIP_INTERVAL=1 XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out" 2>&1 &
wp=$!; sleep 2.5; kill -TERM "$wp"; rc=0; wait "$wp" || rc=$?; sleep 1
assert "ship SIGTERM: exit 143, one final flush, no stray process, lock free" "$([ "$rc" = 143 ] && [ "$(grep -c -e '--final' "$STUB_SHIP_CALLS")" = 1 ] && [ -z "$(pgrep -f -- "$R/scripts/run-bootstrap-local.sh|$R/ingest/.venv/bin/python" || true)" ] && flock -n "$S/cowlic/ingest.lock" true && echo 0 || echo 1)"
assert "ship SIGTERM: the finished line reached the shipped bytes" "$(has 'finished: exit code 143' "$STUB_SHIPPED")"

# 10c. covers (#250): the cover backfill. Caps required without --count, rejected with it; the 21:10 guard and the Grafana
# shipping only for the session; --count is read-only (no guard, no shipping, no --result-json) but takes the lock
new_case cov; run covers --max-new 5 --max-minutes=10 --retry-days 7
assert "covers: exit 0, args forwarded verbatim then --result-json" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS")" = "$(printf '%s\n' covers --max-new 5 --max-minutes=10 --retry-days 7 --result-json "$S/cowlic/.bootstrap-result.json")" ] && echo 0 || echo 1)"
assert "covers: the child sees COWLIC_LOCK_HELD=1 and no fd 9" "$([ "$(has '^lock-held=1$' "$STUB_ARGS.env")" = 0 ] && [ ! -e "$STUB_ARGS.fd9" ] && echo 0 || echo 1)"
assert "covers: last-run says subcommand covers" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["subcommand"]=="covers" and d["exit_code"]==0' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
assert "covers: the session is shipped logs-only under the bootstrap job" "$([ -s "$STUB_SHIP_CALLS" ] && [ "$(grep -vc -e '--logs-only --job cowlic-bootstrap ' "$STUB_SHIP_CALLS" || true)" = 0 ] && grep -q -e '--final' "$STUB_SHIP_CALLS" && echo 0 || echo 1)"
new_case cov-res; STUB_RESULT='{"stop_reason": "cap", "counts": {"covered": 2}}'; export STUB_RESULT; run covers --max-new 5 --max-minutes 10; unset STUB_RESULT
assert "covers: the result object and its stop reason reach last-run" "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["stop_reason"]=="cap" and d["result"]["counts"]=={"covered":2}' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
new_case cov-rc3; STUB_RC=3; export STUB_RC; run covers --max-new 1 --max-minutes 1; unset STUB_RC
assert "covers: exit 3 passed through, sign-in recorded" "$([ "$rc" = 3 ] && python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["stop_reason"]=="sign-in"' "$S/cowlic/bootstrap-last-run.json" > /dev/null 2>&1 && echo 0 || echo 1)"
new_case cov-count; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 21:09:30')" run covers --count --retry-days 3
assert "covers --count: exit 0 even at 21:09:30 (no guard), forwarded verbatim, no --result-json" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS")" = "$(printf '%s\n' covers --count --retry-days 3)" ] && echo 0 || echo 1)"
assert "covers --count: ships nothing" "$([ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1)"
assert "covers --count: last-run written, no offset file" "$([ -s "$S/cowlic/bootstrap-last-run.json" ] && [ ! -e "$S/cowlic/.bootstrap-ship.offset" ] && echo 0 || echo 1)"
new_case cov-count-lock; mkdir -p "$S/cowlic"
( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 3 ) 9> /dev/null &
holder=$!; sleep 0.4
run covers --count
kill "$holder" 2> /dev/null || true; wait "$holder" 2> /dev/null || true
assert "covers --count: a held lock gives 75 and touches nothing" "$([ "$rc" = 75 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"
new_case cov-lock; mkdir -p "$S/cowlic"
( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 3 ) 9> /dev/null &
holder=$!; sleep 0.4
run covers --max-new 1 --max-minutes 1
kill "$holder" 2> /dev/null || true; wait "$holder" 2> /dev/null || true
assert "covers: a held lock gives 75, nothing started, nothing shipped" "$([ "$rc" = 75 ] && [ "$(nothing_started)" = 0 ] && [ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1)"
reject "covers without caps" covers
reject "covers without --max-minutes" covers --max-new 5
reject "covers without --max-new" covers --max-minutes 5
reject "covers --count with --max-new" covers --count --max-new 5
reject "covers --count with --max-minutes=5" covers --count --max-minutes=5
reject "covers --count with both caps" covers --max-new 5 --count --max-minutes 5
reject "covers --max-new 0" covers --max-new 0 --max-minutes 5
reject "covers --max-minutes 1441" covers --max-new 5 --max-minutes 1441
reject "covers abbreviated --max-n" covers --max-n 5 --max-minutes 5
reject "covers --result-json supplied by the caller" covers --max-new 1 --max-minutes 1 --result-json /tmp/x
reject "--count on run" run --count --max-new 1 --max-minutes 1
reject "--count on rank" rank --count
reject "--count on status" status --count
new_case cov-g; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 12:00:00')" run covers --max-new 1 --max-minutes 600
assert "covers: the 21:10 guard applies and names how many minutes fit" "$([ "$rc" = 78 ] && [ "$(has 'at most 538 minutes fit now' "$C/out")" = 0 ] && [ "$(nothing_started)" = 0 ] && echo 0 || echo 1)"
new_case cov-g-ok; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 12:00:00')" run covers --max-new 1 --max-minutes 538
assert "covers: 538 minutes fit at noon" "$(is "$rc" 0)"
new_case cov-g-near; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 21:10:30')" run covers --max-new 1 --max-minutes 1
assert "covers: within 60 s after 21:10 that day's run still counts" "$(is "$rc" 78)"
new_case cov-opt; run covers --max-new 3 --max-minutes 5 --retry-days 0
assert "covers: other options are forwarded untouched" "$([ "$rc" = 0 ] && grep -qx -e '--retry-days' "$STUB_ARGS" && echo 0 || echo 1)"

# 11. mutation checks: break one rule at a time in a copy of the wrapper; the scenario that guards the rule must fail
# on the mutant and pass on the original. A scenario prints 0 when the wrapper behaves correctly.
sc_lock() {
  new_case "m-lock-$RANDOM"; mkdir -p "$S/cowlic"
  ( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 3 ) 9> /dev/null &
  local h=$!; sleep 0.4
  run run --max-new 1 --max-minutes 1
  kill "$h" 2> /dev/null || true; wait "$h" 2> /dev/null || true
  [ "$rc" = 75 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1
}
sc_fd9() { new_case "m-fd9-$RANDOM"; run run --max-new 1 --max-minutes 1; [ ! -e "$STUB_ARGS.fd9" ] && [ ! -e "$STUB_FILTER_ARGS.fd9" ] && [ ! -e "$STUB_SHIP_CALLS.fd9" ] && [ -s "$STUB_SHIP_CALLS" ] && echo 0 || echo 1; }
sc_held() { new_case "m-held-$RANDOM"; run run --max-new 1 --max-minutes 1; grep -qx 'lock-held=1' "$STUB_ARGS.env" && echo 0 || echo 1; }
sc_heldleak() { new_case "m-leak-$RANDOM"; COWLIC_LOCK_HELD=1 run run --max-new 1 --max-minutes 1; grep -qx 'lock-held=unset' "$STUB_FILTER_ARGS.env" && grep -qx 'lock-held=unset' "$STUB_PREREQ_ARGS.env" && echo 0 || echo 1; }
sc_guard() { new_case "m-guard-$RANDOM"; run run --max-new 1 --max-minutes 600; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_guard_next_day() { new_case "m-gnd-$RANDOM"; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 21:11:30')" run run --max-new 1 --max-minutes 60; [ "$rc" = 0 ] && echo 0 || echo 1; }
sc_rc() { new_case "m-rc-$RANDOM"; STUB_RC=3 run run --max-new 1 --max-minutes 1; [ "$rc" = 3 ] && echo 0 || echo 1; }
sc_redact() { new_case "m-red-$RANDOM"; COWLIC_EXTRA_SECRET="$USERNAME_SECRET" run run --max-new 1 --max-minutes 1; grep -q "$USERNAME_SECRET" "$(LOGF)" && echo 1 || echo 0; }
sc_caps() { new_case "m-caps-$RANDOM"; run run; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_range() { new_case "m-range-$RANDOM"; run run --max-new 1000001 --max-minutes 1; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_rank_caps() { new_case "m-rk-$RANDOM"; run rank --max-new 5; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_abbrev() { new_case "m-ab-$RANDOM"; run run --max-new 5 --max-minutes 5 --max-m 99999; [ "$rc" = 78 ] && echo 0 || echo 1; }
sc_logsonly() { # every shipper call that is not the filter is logs-only, job cowlic-bootstrap, nothing of the nightly
  new_case "m-lo-$RANDOM"; run run --max-new 1 --max-minutes 1
  [ -s "$STUB_SHIP_CALLS" ] && [ "$(grep -vc -e '--logs-only --job cowlic-bootstrap ' "$STUB_SHIP_CALLS" || true)" = 0 ] \
    && ! grep -q -e '--last-run' -e '--started-at' -e 'cowlic-ingest' "$STUB_SHIP_CALLS" && echo 0 || echo 1
}
sc_shipped_clean() { # what the shipper would send: this session only, no user name
  new_case "m-sc-$RANDOM"; mkdir -p "$S/cowlic"; printf 'OLD SESSION LINE\n' > "$S/cowlic/bootstrap.log"
  COWLIC_EXTRA_SECRET="$USERNAME_SECRET" run run --max-new 1 --max-minutes 1
  grep -q 'start: run' "$STUB_SHIPPED" && ! grep -q "$USERNAME_SECRET" "$STUB_SHIPPED" && ! grep -q 'OLD SESSION LINE' "$STUB_SHIPPED" && echo 0 || echo 1
}
sc_ship_rc() { new_case "m-srl-$RANDOM"; STUB_SHIP=fail STUB_RC=3 run run --max-new 1 --max-minutes 1; [ "$rc" = 3 ] && echo 0 || echo 1; }
sc_ship_timeout() {
  new_case "m-sto-$RANDOM"; local t=$SECONDS
  COWLIC_SHIP_TIMEOUT=1 STUB_SHIP=hang STUB_RC=5 run run --max-new 1 --max-minutes 1
  [ "$rc" = 5 ] && [ $((SECONDS - t)) -lt 8 ] && echo 0 || echo 1
}
sc_only_run_plan() { new_case "m-orp-$RANDOM"; run status; run rank; [ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1; }
sc_live() {
  new_case "m-live-$RANDOM"; STUB_SLEEP=4 COWLIC_BOOTSTRAP_SHIP_INTERVAL=1 run run --max-new 1 --max-minutes 1
  [ "$(grep -vc -e '--final' "$STUB_SHIP_CALLS" || true)" -ge 2 ] && echo 0 || echo 1
}
sc_tick_orphan() { # after kill -9 of the wrapper the live loop must end by itself: no more shipper calls
  new_case "m-orph-$RANDOM"; STUB_SLEEP=30 COWLIC_BOOTSTRAP_SHIP_INTERVAL=1 XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out" 2>&1 &
  local w=$!; sleep 2.5; kill -9 "$w"; wait "$w" 2> /dev/null || true; sleep 3
  local c1 c2; c1="$(wc -l < "$STUB_SHIP_CALLS")"; sleep 2.5; c2="$(wc -l < "$STUB_SHIP_CALLS")"
  pkill -f -- "$R/scripts/run-bootstrap-local.sh" 2> /dev/null || true
  [ "$c1" -ge 1 ] && [ "$c1" = "$c2" ] && echo 0 || echo 1
}
sc_nightly() {
  new_case "m-night-$RANDOM"; mkdir -p "$S/cowlic"; printf 'n\n' > "$S/cowlic/last-run.json"; printf 'n\n' > "$S/cowlic/ingest.log"
  run run --max-new 1 --max-minutes 1
  [ "$(cat "$S/cowlic/last-run.json")" = n ] && [ "$(cat "$S/cowlic/ingest.log")" = n ] && echo 0 || echo 1
}
sc_lockname() { new_case "m-ln-$RANDOM"; run run --max-new 1 --max-minutes 1; [ -e "$S/cowlic/ingest.lock" ] && echo 0 || echo 1; }
sc_watchdog() {
  sleeper_case "m-wd-$RANDOM"
  XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-local.sh" run --max-new 1 --max-minutes 1 > "$C/out" 2>&1 &
  local w=$!; sleep 1.5; kill -9 "$w"; wait "$w" 2> /dev/null || true; sleep 3.5
  local stray; stray="$(pgrep -f -- "sleep $MARK" || true)"; pkill -f -- "sleep $MARK" 2> /dev/null || true
  [ -z "$stray" ] && echo 0 || echo 1
}
sc_cov_guard() { new_case "m-cg-$RANDOM"; run covers --max-new 1 --max-minutes 600; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_cov_caps() { new_case "m-cc-$RANDOM"; run covers; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_cov_count_caps() { new_case "m-ccc-$RANDOM"; run covers --count --max-new 5; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_cov_count_noguard() { new_case "m-ccg-$RANDOM"; COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch '2026-10-11 21:09:30')" run covers --count; [ "$rc" = 0 ] && [ -e "$STUB_ARGS" ] && echo 0 || echo 1; }
sc_cov_count_noship() { new_case "m-ccs-$RANDOM"; run covers --count; [ "$rc" = 0 ] && [ ! -e "$STUB_SHIP_CALLS" ] && echo 0 || echo 1; }
sc_cov_ship() { new_case "m-cs-$RANDOM"; run covers --max-new 1 --max-minutes 1; [ "$rc" = 0 ] && [ -s "$STUB_SHIP_CALLS" ] && echo 0 || echo 1; }
sc_cov_result() { new_case "m-cr-$RANDOM"; run covers --max-new 1 --max-minutes 1; grep -qx -e '--result-json' "$STUB_ARGS" && echo 0 || echo 1; }
sc_cov_count_result() { new_case "m-ccr-$RANDOM"; run covers --count; ! grep -qx -e '--result-json' "$STUB_ARGS" && echo 0 || echo 1; }
sc_cov_count_lock() {
  new_case "m-ccl-$RANDOM"; mkdir -p "$S/cowlic"
  ( exec 9> "$S/cowlic/ingest.lock"; flock -n 9; exec sleep 3 ) 9> /dev/null &
  local h=$!; sleep 0.4
  run covers --count
  kill "$h" 2> /dev/null || true; wait "$h" 2> /dev/null || true
  [ "$rc" = 75 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1
}
sc_count_on_run() { new_case "m-cor-$RANDOM"; run run --count --max-new 1 --max-minutes 1; [ "$rc" = 78 ] && [ ! -e "$STUB_ARGS" ] && echo 0 || echo 1; }
mutation() { # name, scenario, sed expression: the scenario must pass on the original and fail on the mutant
  local name="$1" sc="$2" expr="$3" orig mut
  WRAPPER_SRC="$ROOT/scripts/run-bootstrap-local.sh"; orig="$($sc)"
  sed "$expr" "$ROOT/scripts/run-bootstrap-local.sh" > "$TMP/mutant.sh"
  if cmp -s "$TMP/mutant.sh" "$ROOT/scripts/run-bootstrap-local.sh"; then assert "mutation $name: the sed expression changed nothing" 1; return; fi
  WRAPPER_SRC="$TMP/mutant.sh"; mut="$($sc)"
  WRAPPER_SRC="$ROOT/scripts/run-bootstrap-local.sh"
  assert "mutation $name: scenario passes on the original and fails on the mutant" "$([ "$orig" = 0 ] && [ "$mut" != 0 ] && echo 0 || echo 1)"
}
mutation "no flock" sc_lock 's/if ! flock -n 9; then/if false; then/'
mutation "wrong lock file" sc_lockname 's#^LOCK="\$STATE_DIR/ingest.lock"#LOCK="$STATE_DIR/bootstrap.lock"#'
mutation "lock fd inherited" sc_fd9 's/ 9>&-//g'
mutation "no COWLIC_LOCK_HELD on the command line" sc_held 's/COWLIC_LOCK_HELD=1 "\$VENV_PY"/"$VENV_PY"/'
mutation "inherited COWLIC_LOCK_HELD not unset" sc_heldleak 's/^unset COWLIC_LOCK_HELD.*$/:/'
mutation "guard disabled" sc_guard 's/-ge "\$NEXT"/-ge 99999999999/'
mutation "guard ignores the next day" sc_guard_next_day 's/ -gt \$((NEXT + 60))/ -gt 99999999999/'
mutation "exit code swallowed" sc_rc 's/exit "\${PIPESTATUS\[0\]}"/exit 0/'
mutation "no redaction filter" sc_redact 's#| "\$VENV_PY" -u "\$SHIP" --filter 9>&- \\#| cat \\#'
mutation "caps not required" sc_caps 's/usage_fail "\$SUB requires/true "$SUB requires/'
mutation "no --max-new range" sc_range 's/\[ "\$val" -gt "\$max" \]/false/'
mutation "rank accepts --max-*" sc_rank_caps 's/rank | status) usage_fail "\$SUB takes no --max-\* option"/rank | status) true/'
mutation "abbreviations accepted" sc_abbrev 's/\*) usage_fail "unknown or abbreviated --m\* option"/*) true/'
mutation "nightly last-run.json overwritten" sc_nightly 's#^LAST_RUN="\$STATE_DIR/bootstrap-last-run.json"#LAST_RUN="$STATE_DIR/last-run.json"#'
mutation "nightly log overwritten" sc_nightly 's#^LOG="\$STATE_DIR/bootstrap.log"#LOG="$STATE_DIR/ingest.log"#'
mutation "shipping drops --logs-only" sc_logsonly 's/--logs-only --job "\$SHIP_JOB"/--job "$SHIP_JOB"/'
mutation "shipping pushes metrics (--last-run, nightly job)" sc_logsonly 's/--logs-only --job "\$SHIP_JOB"/--logs-only --job "$SHIP_JOB" --last-run "$LAST_RUN" --started-at "$STARTED_AT"/'
mutation "shipping under the nightly job label" sc_logsonly 's/^SHIP_JOB="cowlic-bootstrap"/SHIP_JOB="cowlic-ingest"/'
mutation "shipping from offset 0 (earlier sessions)" sc_shipped_clean 's/--offset "\$LOG_OFFSET"/--offset 0/'
mutation "shipped bytes not redacted" sc_shipped_clean 's#| "\$VENV_PY" -u "\$SHIP" --filter 9>&- \\#| cat \\#'
mutation "shipper failure changes the exit code" sc_ship_rc 's/out="\$(ship_call --final)" || out=""/out="$(ship_call --final)" || exit 9/'
mutation "shipper without timeout" sc_ship_timeout 's/timeout -k 2 "\$SHIP_TIMEOUT_S" //'
mutation "shipping for every subcommand" sc_only_run_plan '/^# The live shipper/,/^esac/ s/^  1)$/  *)/'
mutation "no live loop" sc_live 's/^      warned=0$/      exit 0/'
mutation "live loop outlives the wrapper" sc_tick_orphan 's#kill -0 "\$WRAPPER_PID" 2> /dev/null || exit 0#true#'
mutation "no watchdog" sc_watchdog 's/^  while kill -0 "\$WRAPPER_PID".*$/  sleep 100000/'
mutation "covers is not guarded against 21:10" sc_cov_guard 's/^case "\$SUB" in run | plan) LONG=1 ;; covers) \[ "\$COUNT" = 1 \] || LONG=1 ;; esac/case "$SUB" in run | plan) LONG=1 ;; esac/'
mutation "covers caps not required" sc_cov_caps 's/usage_fail "covers requires --max-new and --max-minutes (or --count)"/true/'
mutation "covers --count accepts caps" sc_cov_count_caps 's/usage_fail "covers --count takes no --max-new or --max-minutes"/true/'
mutation "covers --count is guarded" sc_cov_count_noguard 's/covers) \[ "\$COUNT" = 1 \] || LONG=1 ;;/covers) LONG=1 ;;/'
mutation "covers --count is shipped" sc_cov_count_noship 's/covers) \[ "\$COUNT" = 1 \] || LONG=1 ;;/covers) LONG=1 ;;/'
mutation "covers session is not shipped" sc_cov_ship 's/^case "\$SUB" in run | plan) LONG=1 ;; covers) \[ "\$COUNT" = 1 \] || LONG=1 ;; esac/case "$SUB" in run | plan) LONG=1 ;; esac/'
mutation "covers gets no --result-json" sc_cov_result 's/if \[ "\$SUB" = run \] || { \[ "\$SUB" = covers \] \&\& \[ "\$COUNT" = 0 \]; }; then/if [ "$SUB" = run ]; then/'
mutation "covers --count gets --result-json" sc_cov_count_result 's/ \&\& \[ "\$COUNT" = 0 \]; }; then EXTRA/; }; then EXTRA/'
mutation "covers --count skips the lock" sc_cov_count_lock 's/^exec 9> "\$LOCK"$/if [ "$COUNT" = 1 ]; then LOCK=\/dev\/null; fi; exec 9> "$LOCK"/'
mutation "--count accepted on run" sc_count_on_run 's/\[ "\$SUB" = covers \] || usage_fail "--count belongs to covers only"/true/'

if [ "$failures" -ne 0 ]; then echo "$failures FAILED"; exit 1; fi
echo "all passed"

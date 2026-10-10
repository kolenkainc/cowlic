#!/usr/bin/env bash
# Test for run-lastfm-report.sh on a fake repo copy with a stub venv python (stub report, stub redaction filter,
# stub shipper). Never runs the real report, never touches the network.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failures=0
assert() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi; }
has() { grep -q -- "$1" "$2" 2> /dev/null && echo 0 || echo 1; }
hasnt() { grep -q -- "$1" "$2" 2> /dev/null && echo 1 || echo 0; }

USERNAME_SECRET="sentinel_lfm_user"
KEY_SECRET="SENTINELKEY0123456789"

new_case() {
  C="$TMP/$1"; R="$C/root"; S="$C/state"
  mkdir -p "$R/scripts" "$R/ingest/.venv/bin" "$S"
  cp "$ROOT/scripts/run-lastfm-report.sh" "$R/scripts/"
  : > "$R/ingest/lastfm_report.py"; : > "$R/ingest/ship_grafana.py"
  cat > "$R/ingest/.venv/bin/python" <<'PY'
#!/bin/sh
case "$*" in
  *ship_grafana.py*--filter*)  # stub redaction: removes the extra secret from stdin
    exec sed "s/$COWLIC_EXTRA_SECRET/***/g" ;;
  *ship_grafana.py*)
    echo "$@" >> "$STUB_SHIP_CALLS"
    [ -z "${COWLIC_EXTRA_SECRET:-}" ] || echo "extra-secret-in-env" >> "$STUB_SHIP_CALLS.env"
    if [ -e /proc/self/fd/9 ]; then echo leaked >> "$STUB_SHIP_CALLS.fd9"; fi
    case "${STUB_SHIP:-ok}" in
      ok) echo "grafana: loki push ok (HTTP 204)"; exit 0 ;;
      fail) echo "grafana: boom"; exit 1 ;;
      kill) kill -9 $$ ;;
      hang) sleep 300; exit 0 ;;
    esac ;;
  *lastfm_report.py*)
    shift
    echo "$@" > "$STUB_REPORT_ARGS"
    echo "report line for user $COWLIC_EXTRA_SECRET and more"
    echo "report error on stderr" >&2
    [ -z "${STUB_SLEEP:-}" ] || exec sleep "$STUB_SLEEP"
    exit "${STUB_RC:-0}" ;;
esac
PY
  chmod +x "$R/scripts/"*.sh "$R/ingest/.venv/bin/python"
  export STUB_SHIP_CALLS="$C/ship_calls" STUB_REPORT_ARGS="$C/report_args"
}
run() { rc=0; XDG_STATE_HOME="$S" bash "$R/scripts/run-lastfm-report.sh" "$@" > "$C/out" 2>&1 || rc=$?; }
LOGF() { echo "$S/cowlic/lastfm-report.log"; }

# 1. success path, exit code passthrough, redaction of the user name, no metrics / last-run
new_case ok; STUB_RC=3; export STUB_RC
run --lastfm-user "$USERNAME_SECRET" --years 2
assert "exit code of the report is passed through" "$([ "$rc" = 3 ] && echo 0 || echo 1)"
assert "arguments are forwarded to the report" "$(has "--lastfm-user $USERNAME_SECRET --years 2" "$STUB_REPORT_ARGS")"
assert "log has the report output (stdout and stderr)" "$(has 'report error on stderr' "$(LOGF)")"
assert "user name is redacted in the log" "$(hasnt "$USERNAME_SECRET" "$(LOGF)")"
assert "user name is not on the terminal output" "$(hasnt "$USERNAME_SECRET" "$C/out")"
assert "log is mode 0600" "$([ "$(stat -c %a "$(LOGF)")" = 600 ] && echo 0 || echo 1)"
assert "state dir is private" "$([ "$(stat -c %a "$S/cowlic")" = 700 ] && echo 0 || echo 1)"
assert "final push: logs only, job cowlic-report, --final" "$(has '--logs-only --job cowlic-report .*--final' "$STUB_SHIP_CALLS")"
assert "shipper never gets --last-run or the user name in argv" "$(hasnt "--last-run\\|$USERNAME_SECRET" "$STUB_SHIP_CALLS")"
assert "the user name reaches the shipper through the environment" "$(has extra-secret-in-env "$STUB_SHIP_CALLS.env")"
assert "no last-run.json is written" "$([ ! -e "$S/cowlic/last-run.json" ] && echo 0 || echo 1)"
assert "lock fd is not inherited by the shipper" "$([ ! -e "$STUB_SHIP_CALLS.fd9" ] && echo 0 || echo 1)"
assert "offset file is removed at the end" "$([ ! -e "$S/cowlic/.lastfm-report.offset" ] && echo 0 || echo 1)"
assert "the arguments are not logged" "$(hasnt "--years" "$(LOGF)")"
unset STUB_RC

# 2. --lastfm-user=NAME form is redacted too; bash -x leaks nothing
new_case eq
rc=0; XDG_STATE_HOME="$S" bash -x "$R/scripts/run-lastfm-report.sh" "--lastfm-user=$USERNAME_SECRET" > "$C/out" 2>&1 || rc=$?
assert "--lastfm-user=NAME: success" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "--lastfm-user=NAME: redacted in the log" "$(hasnt "$USERNAME_SECRET" "$(LOGF)")"
assert "bash -x output has no user name" "$(hasnt "$USERNAME_SECRET" "$C/out")"

# 3. shipper failures never change the exit code
for mode in fail kill; do
  new_case "ship-$mode"; STUB_SHIP="$mode"; export STUB_SHIP; STUB_RC=0; export STUB_RC
  run --lastfm-user "$USERNAME_SECRET"
  assert "shipper $mode: exit code stays 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
done
new_case ship-hang; STUB_SHIP=hang; export STUB_SHIP; STUB_RC=5; export STUB_RC
COWLIC_SHIP_TIMEOUT=1; export COWLIC_SHIP_TIMEOUT
start=$SECONDS; run --lastfm-user "$USERNAME_SECRET"
assert "shipper hang: killed by the timeout, exit code stays 5" "$([ "$rc" = 5 ] && [ $((SECONDS - start)) -lt 20 ] && echo 0 || echo 1)"
unset STUB_SHIP STUB_RC COWLIC_SHIP_TIMEOUT

# 4. live shipping: at least one tick before the final push while the report is still running
new_case live; STUB_SLEEP=4; export STUB_SLEEP; COWLIC_REPORT_SHIP_INTERVAL=1; export COWLIC_REPORT_SHIP_INTERVAL
run --lastfm-user "$USERNAME_SECRET"
ticks="$(grep -vc -- '--final' "$STUB_SHIP_CALLS" || true)"
assert "live shipping: ticks before the final push" "$([ "$ticks" -ge 2 ] && echo 0 || echo 1)"
assert "live ticks carry the offset file" "$(has '--offset-file' "$STUB_SHIP_CALLS")"
unset STUB_SLEEP COWLIC_REPORT_SHIP_INTERVAL

# 5. a second report while one runs: 75, nothing else touched
new_case lock; STUB_SLEEP=5; export STUB_SLEEP
( XDG_STATE_HOME="$S" bash "$R/scripts/run-lastfm-report.sh" --lastfm-user x > "$C/out1" 2>&1 ) &
first=$!
sleep 1.5
rc=0; XDG_STATE_HOME="$S" bash "$R/scripts/run-lastfm-report.sh" --lastfm-user x > "$C/out2" 2>&1 || rc=$?
assert "second report is refused with 75" "$([ "$rc" = 75 ] && echo 0 || echo 1)"
wait "$first" || true
unset STUB_SLEEP

# 6. SIGTERM to the wrapper: report and live shipper are stopped, no stray process, exit 143
new_case term; MARK="sleep-marker-$$-$RANDOM"
cat > "$R/ingest/.venv/bin/python" <<PY
#!/bin/sh
case "\$*" in
  *ship_grafana.py*--filter*) exec cat ;;
  *ship_grafana.py*) echo "\$@" >> "$C/ship_calls"; exec sleep 0.2 ;;
  *lastfm_report.py*) echo started; exec sleep 3$RANDOM ;;
esac
PY
export COWLIC_REPORT_SHIP_INTERVAL=1
XDG_STATE_HOME="$S" bash "$R/scripts/run-lastfm-report.sh" --lastfm-user x > "$C/out" 2>&1 &
wp=$!
sleep 2.5
kill -TERM "$wp"
rc=0; wait "$wp" || rc=$?
sleep 1
assert "SIGTERM: exit code 143" "$([ "$rc" = 143 ] && echo 0 || echo 1)"
stray="$(pgrep -f -- "$R/ingest/.venv/bin/python|$R/scripts/run-lastfm-report.sh" || true)"
assert "SIGTERM: no stray process" "$([ -z "$stray" ] && echo 0 || echo 1)"
assert "SIGTERM: final push still happened" "$(has '--final' "$C/ship_calls")"
unset COWLIC_REPORT_SHIP_INTERVAL

# 7. missing venv: 78
new_case novenv; rm -f "$R/ingest/.venv/bin/python"; run --lastfm-user x
assert "no venv: exit 78" "$([ "$rc" = 78 ] && echo 0 || echo 1)"

if [ "$failures" -ne 0 ]; then echo "$failures FAILED"; exit 1; fi
echo "all passed"

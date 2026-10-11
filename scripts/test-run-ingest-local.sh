#!/usr/bin/env bash
# Test for run-ingest-local.sh on a fake repo copy in a temp dir with a stub venv python and a stub
# prerequisites script. Never runs the real ingest, spotdl or network.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failures=0
assert() { # name, condition result
  if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi
}

# new_case NAME: fake repo with the wrapper, a stub prereqs script, a stub venv python and a state dir.
new_case() {
  C="$TMP/$1"; R="$C/root"; S="$C/state"
  mkdir -p "$R/scripts" "$R/ingest/.venv/bin" "$S"
  cp "$ROOT/scripts/run-ingest-local.sh" "$R/scripts/"
  # Stub prereqs: STUB_PREREQ_FAIL = another prerequisite missing (fatal); STUB_DENO_MISSING = only deno
  # missing: WARN with --deno-optional (exit 0), MISSING without it (exit 1), like the real script.
  cat > "$R/scripts/check-local-prereqs.sh" <<'SH'
#!/bin/sh
echo "$@" > "$STUB_PREREQ_ARGS_FILE"
[ -z "${STUB_PREREQ_FAIL:-}" ] || { echo "MISSING  stub: fix it"; exit 1; }
if [ -n "${STUB_DENO_MISSING:-}" ]; then
  if [ "${1:-}" = --deno-optional ]; then echo "WARN     deno: not on PATH"; else echo "MISSING  deno: not on PATH"; exit 1; fi
fi
echo "All prerequisites present."
SH
  export STUB_PREREQ_ARGS_FILE="$C/prereq_args"
  # Stub python variant below also reports whether the lock fd 9 leaked into the child.
  # Stub python: records its args, prints a line, optionally sleeps, exits with STUB_RC.
  cat > "$R/ingest/.venv/bin/python" <<'PY'
#!/bin/sh
[ "${1:-}" != -I ] || exec python3 "$@"  # the wrapper validates the result JSON with `python -I -c ...`
case "${1:-}" in
  */ship_grafana.py)  # the Grafana shipper: records its call, then behaves as STUB_SHIP says
    echo "$@" > "$STUB_SHIP_ARGS_FILE"
    cp "$STUB_LAST_RUN_FILE" "$STUB_SHIP_ARGS_FILE.lastrun" 2>/dev/null || echo missing > "$STUB_SHIP_ARGS_FILE.lastrun"
    if [ -e /proc/self/fd/9 ]; then echo leaked > "$STUB_SHIP_ARGS_FILE.fd9"; fi
    case "${STUB_SHIP:-ok}" in
      ok) echo "grafana: loki push ok (HTTP 204)"; exit 0 ;;
      fail) echo "grafana: boom"; exit 1 ;;
      kill) kill -9 $$ ;;
      hang) sleep 300; exit 0 ;;
      noisy) printf 'grafana: SECRET$(touch %s/pwned)`x` \033[31m%0300d\nsecond line\n' "$STUB_SHIP_ARGS_FILE.d" 0; exit 0 ;;
      silent) exit 0 ;;
      exit0-stderr) echo "Traceback: token=SENTINEL_SHIP_STDERR" >&2; exit 0 ;;
    esac ;;
esac
echo "$@" > "$STUB_ARGS_FILE"
if [ -e /proc/self/fd/9 ]; then echo leaked > "$STUB_ARGS_FILE.fd9"; fi
echo "stub ingest output"
# --result-json PATH: write STUB_RESULT (a JSON object) there unless the stub plays a crash (no result).
prev=""
for a in "$@"; do
  [ "$prev" = --result-json ] && [ -n "${STUB_RESULT:-}" ] && printf '%s\n' "$STUB_RESULT" > "$a"
  prev="$a"
done
[ -z "${STUB_SLEEP:-}" ] || sleep "$STUB_SLEEP"
exit "${STUB_RC:-0}"
PY
  chmod +x "$R/scripts/"*.sh "$R/ingest/.venv/bin/python"
  export STUB_ARGS_FILE="$C/args" STUB_SHIP_ARGS_FILE="$C/ship_args" STUB_LAST_RUN_FILE="$S/cowlic/last-run.json"
  : > "$R/ingest/ship_grafana.py"  # the wrapper only checks that it exists; the stub python plays it
}
run() { # runs the wrapper, output to $C/out, sets rc
  rc=0; XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out" 2>&1 || rc=$?
}
LOGF() { echo "$S/cowlic/ingest.log"; }

# 1. success: exit 0, args, log has ingest output
new_case ok; STUB_RC=0; export STUB_RC; run
assert "success exit 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "ingest gets --sources sources.yml" "$(grep -q -- "ingest.py --sources $R/sources.yml" "$STUB_ARGS_FILE" && echo 0 || echo 1)"
assert "log has ingest output" "$(grep -q 'stub ingest output' "$(LOGF)" && echo 0 || echo 1)"
assert "log has start and finished lines" "$(grep -q 'finished: exit code 0, ok' "$(LOGF)" && echo 0 || echo 1)"
assert "state dir is private" "$([ "$(stat -c %a "$S/cowlic")" = 700 ] && echo 0 || echo 1)"

assert "python runs unbuffered (-u)" "$(grep -q -- '^-u ' "$STUB_ARGS_FILE" && echo 0 || echo 1)"
assert "prereqs called with --deno-optional" "$(grep -qx -- '--deno-optional' "$STUB_PREREQ_ARGS_FILE" && echo 0 || echo 1)"
assert "lock fd not inherited by ingest" "$([ ! -e "$STUB_ARGS_FILE.fd9" ] && echo 0 || echo 1)"

# 1b. only deno missing: ingest still runs, warning logged, exit 0
new_case deno; STUB_DENO_MISSING=1; export STUB_DENO_MISSING; run
assert "deno missing: exit 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "deno missing: ingest ran" "$([ -s "$STUB_ARGS_FILE" ] && echo 0 || echo 1)"
assert "deno missing: warning in log" "$(grep -q 'prerequisites: WARN     deno' "$(LOGF)" && echo 0 || echo 1)"
unset STUB_DENO_MISSING

# 2. non-zero ingest exit codes pass through (including one through the pipe to tee)
for code in 1 3 42; do
  new_case "fail$code"; STUB_RC=$code; export STUB_RC; run
  assert "exit code $code passed through" "$([ "$rc" = "$code" ] && echo 0 || echo 1)"
  assert "code $code logged as failed" "$(grep -q "exit code $code, failed" "$(LOGF)" && echo 0 || echo 1)"
done
STUB_RC=0; export STUB_RC

# 3. lock contention: second run exits 75 without running ingest
new_case lock; STUB_SLEEP=3; export STUB_SLEEP
( XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out1" 2>&1 ) &
bg=$!
for _ in $(seq 50); do [ -s "$STUB_ARGS_FILE" ] && break; sleep 0.1; done
rm -f "$STUB_ARGS_FILE"
run
assert "locked run exits 75" "$([ "$rc" = 75 ] && echo 0 || echo 1)"
assert "locked run logs it" "$(grep -q 'another run is active' "$C/out" && echo 0 || echo 1)"
assert "locked run did not start ingest" "$([ ! -e "$STUB_ARGS_FILE" ] && echo 0 || echo 1)"
wait "$bg" && assert "first run unaffected" 0 || assert "first run unaffected" 1
unset STUB_SLEEP
run
assert "lock released after the run" "$([ "$rc" = 0 ] && echo 0 || echo 1)"

# 4. missing venv: exit 78, no ingest
new_case novenv; rm -rf "$R/ingest/.venv"; run
assert "missing venv exits 78" "$([ "$rc" = 78 ] && echo 0 || echo 1)"
assert "missing venv logs hint" "$(grep -q 'ingest/.venv not found' "$(LOGF)" && echo 0 || echo 1)"

# 5. prerequisites failure: exit 78, ingest not run
new_case prereq; STUB_PREREQ_FAIL=1; export STUB_PREREQ_FAIL; run
assert "failed prereqs exit 78" "$([ "$rc" = 78 ] && echo 0 || echo 1)"
assert "failed prereqs logged" "$(grep -q 'MISSING  stub' "$(LOGF)" && echo 0 || echo 1)"
assert "failed prereqs did not start ingest" "$([ ! -e "$STUB_ARGS_FILE" ] && echo 0 || echo 1)"
unset STUB_PREREQ_FAIL
new_case prereq2; STUB_PREREQ_FAIL=1; STUB_DENO_MISSING=1; export STUB_PREREQ_FAIL STUB_DENO_MISSING; run
assert "other missing prereq plus deno: still 78" "$([ "$rc" = 78 ] && echo 0 || echo 1)"
unset STUB_PREREQ_FAIL STUB_DENO_MISSING

# 5b. unset HOME and XDG_STATE_HOME: clear message, exit 78
new_case nohome; rc=0
env -u HOME -u XDG_STATE_HOME bash "$R/scripts/run-ingest-local.sh" > "$C/out" 2>&1 || rc=$?
assert "no HOME: exit 78 with message" "$([ "$rc" = 78 ] && grep -q 'neither XDG_STATE_HOME nor HOME' "$C/out" && echo 0 || echo 1)"

# 6. rotation by size, keeping at most COWLIC_LOG_KEEP files
new_case rot; mkdir -p "$S/cowlic"
export COWLIC_LOG_MAX_BYTES=100 COWLIC_LOG_KEEP=2
for _ in 1 2 3 4 5; do run; done
assert "rotated file .1 exists" "$([ -f "$(LOGF).1" ] && echo 0 || echo 1)"
assert "rotated file .2 exists" "$([ -f "$(LOGF).2" ] && echo 0 || echo 1)"
assert "no file beyond keep count" "$([ ! -e "$(LOGF).3" ] && echo 0 || echo 1)"
assert "current log exists" "$([ -f "$(LOGF)" ] && echo 0 || echo 1)"
unset COWLIC_LOG_MAX_BYTES COWLIC_LOG_KEEP

# 7. no value leak with xtrace on (bash -x and SHELLOPTS), wrapper never touches .env
new_case xtrace; printf 'TG_BOT_TOKEN=%s\n' "SENTINEL_ENV_FILE_VALUE" > "$R/.env"
rc=0
SENTINEL_SECRET="SENTINEL_ENV_VALUE" XDG_STATE_HOME="$S" bash -x "$R/scripts/run-ingest-local.sh" > "$C/out" 2>&1 || rc=$?
assert "xtrace run succeeds" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "no env value in output with bash -x" "$(grep -q SENTINEL "$C/out" && echo 1 || echo 0)"
assert "no env value in log with bash -x" "$(grep -q SENTINEL "$(LOGF)" && echo 1 || echo 0)"
rm -f "$(LOGF)"; rc=0
env SENTINEL_SECRET="SENTINEL_ENV_VALUE" SHELLOPTS=xtrace XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out" 2>&1 || rc=$?
assert "no env value in output with SHELLOPTS=xtrace" "$(grep -q SENTINEL "$C/out" && echo 1 || echo 0)"
assert "wrapper does not read .env" "$(grep -Eq '(\.|source)[[:space:]].*\.env|cat .*\.env' "$ROOT/scripts/run-ingest-local.sh" && echo 1 || echo 0)"

# 8. last-run.json
LASTF() { echo "$S/cowlic/last-run.json"; }
jget() { python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d=d[k]
print(json.dumps(d) if isinstance(d,(dict,list)) or d is None else d)' "$(LASTF)" "$1"; }
valid_json() { python3 -I -c 'import json,sys; json.load(open(sys.argv[1]))' "$(LASTF)" 2>/dev/null; }
RES_OK='{"failure_class": "none", "exit_code": 0, "playlists": [{"url": "https://open.spotify.com/playlist/AAAAAAAAAAAAAAAAAAAAAA", "status": "done", "added": 2, "skipped": 1, "no_match": 0, "too_big": 0, "failed": 0, "not_attempted": 0}], "counts": {"added": 2, "skipped": 1, "no_match": 0, "too_big": 0, "failed": 0, "not_attempted": 0, "playlists_unreadable": 0, "playlists_not_attempted": 0}}'
res_with() { printf '%s' "$RES_OK" | sed "s/\"failure_class\": \"none\"/\"failure_class\": \"$1\"/"; }
ISO='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

new_case lr_ok; STUB_RC=0; STUB_RESULT="$RES_OK"; export STUB_RC STUB_RESULT
SENTINEL_SECRET="SENTINEL_ENV_VALUE" XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out" 2>&1 || true
assert "last-run: written, valid JSON" "$(valid_json && echo 0 || echo 1)"
assert "last-run: mode 0600" "$([ "$(stat -c %a "$(LASTF)")" = 600 ] && echo 0 || echo 1)"
assert "last-run: class none, exit 0" "$([ "$(jget failure_class)" = none ] && [ "$(jget exit_code)" = 0 ] && echo 0 || echo 1)"
assert "last-run: UTC timestamps" "$([[ "$(jget started_at)" =~ $ISO ]] && [[ "$(jget finished_at)" =~ $ISO ]] && echo 0 || echo 1)"
assert "last-run: log file name only" "$([ "$(jget log_file)" = ingest.log ] && echo 0 || echo 1)"
assert "last-run: playlists and counts from ingest" "$([ "$(jget result.counts.added)" = 2 ] && grep -q 'playlist/AAAAAAAAAAAAAAAAAAAAAA' "$(LASTF)" && echo 0 || echo 1)"
assert "last-run: no state dir path or secret in it" "$(grep -qE "SENTINEL|$S|/home/" "$(LASTF)" && echo 1 || echo 0)"
assert "last-run: no temp or result file left" "$([ -z "$(ls -A "$S/cowlic" | grep -E '^\.(last-run|ingest-result)')" ] && echo 0 || echo 1)"

new_case lr_partial; STUB_RC=1; STUB_RESULT="$(res_with partial)"; export STUB_RC STUB_RESULT; run
assert "last-run: partial" "$([ "$(jget failure_class)" = partial ] && [ "$(jget exit_code)" = 1 ] && echo 0 || echo 1)"

# atomic: the old file is replaced by a rename (new inode), never rewritten in place
STUB_RC=0; STUB_RESULT="$RES_OK"; export STUB_RC STUB_RESULT
new_case lr_atomic; mkdir -p "$S/cowlic"; echo '{"old": true}' > "$(LASTF)"; ino_before="$(stat -c %i "$(LASTF)")"; run
assert "last-run: replaced by rename, not rewritten in place" "$([ "$(stat -c %i "$(LASTF)")" != "$ino_before" ] && valid_json && echo 0 || echo 1)"

new_case lr_signin; STUB_RC=3; STUB_RESULT="$(res_with youtube-sign-in)"; export STUB_RC STUB_RESULT; run
assert "last-run: youtube-sign-in keeps counts" "$([ "$(jget failure_class)" = youtube-sign-in ] && [ "$(jget exit_code)" = 3 ] && [ "$(jget result.counts.added)" = 2 ] && echo 0 || echo 1)"

new_case lr_crash; STUB_RC=1; STUB_RESULT=""; export STUB_RC STUB_RESULT
mkdir -p "$S/cowlic"; printf '%s\n' "$(res_with partial)" > "$S/cowlic/.ingest-result.json"  # stale file of an earlier run
run
assert "last-run: crash (no result), stale result not used" "$([ "$(jget failure_class)" = crash ] && [ "$(jget result)" = null ] && echo 0 || echo 1)"

new_case lr_garbage; STUB_RC=1; STUB_RESULT="not json, class partial"; export STUB_RC STUB_RESULT; run
assert "last-run: garbage result is dropped, still valid JSON" "$(valid_json && [ "$(jget result)" = null ] && [ "$(jget failure_class)" = crash ] && echo 0 || echo 1)"

for bad in '{garbage}' "$RES_OK"$'\n'"$RES_OK" "$RES_OK junk}"; do
  new_case lr_bad; STUB_RC=1; STUB_RESULT="$bad"; export STUB_RC STUB_RESULT; run
  assert "last-run: invalid result ($(printf '%.12s' "$bad")) gives valid JSON, result null, crash" "$(valid_json && [ "$(jget result)" = null ] && [ "$(jget failure_class)" = crash ] && echo 0 || echo 1)"
done

STUB_RC=0; STUB_RESULT=""; export STUB_RC STUB_RESULT
new_case lr_novenv; rm -rf "$R/ingest/.venv"; run
assert "last-run: missing venv is config, no result" "$([ "$(jget failure_class)" = config ] && [ "$(jget exit_code)" = 78 ] && [ "$(jget result)" = null ] && echo 0 || echo 1)"
new_case lr_prereq; STUB_PREREQ_FAIL=1; export STUB_PREREQ_FAIL; run; unset STUB_PREREQ_FAIL
assert "last-run: failed prerequisites is config" "$([ "$(jget failure_class)" = config ] && echo 0 || echo 1)"

# lock contention: the blocked invocation leaves last-run.json and the running run's log alone
new_case lr_lock; mkdir -p "$S/cowlic"; echo '{"sentinel": true}' > "$(LASTF)"; STUB_SLEEP=3; export STUB_SLEEP
( XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out1" 2>&1 ) &
bg=$!
for _ in $(seq 50); do [ -s "$STUB_ARGS_FILE" ] && break; sleep 0.1; done
log_before="$(cat "$(LOGF)")"
run
assert "last-run: locked run exits 75" "$([ "$rc" = 75 ] && echo 0 || echo 1)"
assert "last-run: locked run leaves it untouched" "$([ "$(cat "$(LASTF)")" = '{"sentinel": true}' ] && echo 0 || echo 1)"
assert "last-run: locked run does not touch the running log" "$([ "$(cat "$(LOGF)")" = "$log_before" ] && echo 0 || echo 1)"
wait "$bg" || true
unset STUB_SLEEP
assert "last-run: replaced by the run that held the lock" "$(valid_json && [ "$(jget exit_code)" = 0 ] && echo 0 || echo 1)"
unset STUB_RESULT

# 9. Grafana shipping: best effort, never changes the exit code or last-run.json, never hangs the run
STUB_RC=0; STUB_RESULT=""; export STUB_RC STUB_RESULT
new_case ship_ok; mkdir -p "$S/cowlic"; printf 'earlier run line\nsecond earlier line\n' > "$(LOGF)"; pre_size="$(wc -c < "$(LOGF)")"
export STUB_SHIP=ok; run
assert "ship: shipper called with log, offset, last-run, started-at" "$(grep -q -- "ship_grafana.py --log $(LOGF) --offset $pre_size --last-run $(LASTF) --started-at 20" "$STUB_SHIP_ARGS_FILE" && echo 0 || echo 1)"
assert "ship: offset is the log size before the run" "$([ "$pre_size" -gt 0 ] && echo 0 || echo 1)"
assert "ship: last-run.json already written when the shipper runs" "$(python3 -I -c 'import json,sys; json.load(open(sys.argv[1]))' "$STUB_SHIP_ARGS_FILE.lastrun" && echo 0 || echo 1)"
assert "ship: lock fd not inherited by the shipper" "$([ ! -e "$STUB_SHIP_ARGS_FILE.fd9" ] && echo 0 || echo 1)"
assert "ship: its one status line is logged" "$(grep -q 'run-ingest-local: grafana: loki push ok (HTTP 204)' "$(LOGF)" && echo 0 || echo 1)"
assert "ship: exit 0 unchanged" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
new_case ship_first; export STUB_SHIP=ok; run  # no log yet: offset 0
assert "ship: first run has offset 0" "$(grep -q -- '--offset 0 ' "$STUB_SHIP_ARGS_FILE" && echo 0 || echo 1)"
assert "ship: no error output for a missing log" "$(grep -q 'No such file' "$C/out" && echo 1 || echo 0)"

for mode in ok fail kill silent noisy exit0-stderr; do
  for code in 0 1 3; do
    new_case "ship_${mode}_$code"; STUB_RC=$code; STUB_SHIP=$mode; export STUB_RC STUB_SHIP
    [ "$code" = 0 ] || STUB_RESULT="$(res_with partial)"; export STUB_RESULT; run; STUB_RESULT=""
    assert "ship $mode: exit code $code unchanged" "$([ "$rc" = "$code" ] && echo 0 || echo 1)"
    assert "ship $mode: last-run.json valid with exit_code $code" "$(valid_json && [ "$(jget exit_code)" = "$code" ] && echo 0 || echo 1)"
    assert "ship $mode: lock released" "$(flock -n "$S/cowlic/ingest.lock" true && echo 0 || echo 1)"
  done
done
new_case ship_fail_log; STUB_RC=0 STUB_SHIP=fail; export STUB_RC STUB_SHIP; run
assert "ship fail: one WARN line, stub output not logged" "$(grep -q 'WARN grafana shipping failed' "$(LOGF)" && ! grep -q 'grafana: boom' "$(LOGF)" && echo 0 || echo 1)"
new_case ship_kill_log; STUB_SHIP=kill; export STUB_SHIP; run
assert "ship killed by a signal: WARN line, exit 0" "$([ "$rc" = 0 ] && grep -q 'WARN grafana shipping' "$(LOGF)" && echo 0 || echo 1)"
new_case ship_noisy; STUB_SHIP=noisy; export STUB_SHIP; run
assert "ship noisy: only a sanitized first line is logged" "$(grep -q 'run-ingest-local: grafana: SECRET' "$(LOGF)" && ! grep -q 'second line' "$(LOGF)" && ! grep -q "$(printf '\033')" "$(LOGF)" && ! grep -q '\$(' "$(LOGF)" && ! grep -q '`' "$(LOGF)" && echo 0 || echo 1)"
assert "ship noisy: nothing executed from its output" "$([ ! -e "$STUB_SHIP_ARGS_FILE.d/pwned" ] && echo 0 || echo 1)"
assert "ship noisy: logged line is bounded" "$([ "$(grep 'grafana: SECRET' "$(LOGF)" | head -1 | wc -c)" -lt 300 ] && echo 0 || echo 1)"
new_case ship_stderr; STUB_SHIP=exit0-stderr; export STUB_SHIP; run
assert "ship: its stderr is dropped" "$(grep -rq SENTINEL_SHIP_STDERR "$C/out" "$(LOGF)" && echo 1 || echo 0)"
new_case ship_silent; STUB_SHIP=silent; export STUB_SHIP; run
assert "ship silent: no extra line, exit 0" "$([ "$rc" = 0 ] && ! grep -q 'run-ingest-local: grafana' "$(LOGF)" && echo 0 || echo 1)"

# a hanging shipper is killed by the timeout and the run still ends with the ingest exit code
new_case ship_hang; STUB_RC=1; STUB_SHIP=hang; STUB_RESULT="$(res_with partial)"; export STUB_RC STUB_SHIP STUB_RESULT
run_start=$SECONDS; export COWLIC_SHIP_TIMEOUT=2; run; elapsed=$((SECONDS - run_start)); unset COWLIC_SHIP_TIMEOUT
assert "ship hang: killed by timeout, run ends within seconds" "$([ "$elapsed" -lt 20 ] && echo 0 || echo 1)"
assert "ship hang: ingest exit code 1 unchanged" "$([ "$rc" = 1 ] && echo 0 || echo 1)"
assert "ship hang: timed out line logged" "$(grep -q 'WARN grafana shipping timed out' "$(LOGF)" && echo 0 || echo 1)"
assert "ship hang: last-run.json intact" "$(valid_json && [ "$(jget exit_code)" = 1 ] && echo 0 || echo 1)"
assert "ship hang: lock released" "$(flock -n "$S/cowlic/ingest.lock" true && echo 0 || echo 1)"
assert "ship: default hard limit is 30 s" "$(grep -q 'COWLIC_SHIP_TIMEOUT:-30' "$ROOT/scripts/run-ingest-local.sh" && echo 0 || echo 1)"
unset STUB_SHIP STUB_RESULT

# COWLIC_SHIP_TIMEOUT: only a positive integer up to 120 is honoured, anything else (0 means "no limit" to
# timeout) falls back to 30. A recording timeout shim in front of the real one shows the limit that was used.
REAL_TIMEOUT="$(command -v timeout)"; ORIG_PATH="$PATH"
for pair in "0:30" "00:30" "abc:30" "-5:30" "1.5:30" "121:30" "99999999999999999999:30" "5:5" "120:120" ":30"; do
  val="${pair%:*}"; want="${pair##*:}"
  new_case "ship_to_$val"; mkdir -p "$C/bin"
  printf '#!/bin/sh\n[ "$3" = "%s" ] && echo "$@" > "%s"\nexec "%s" "$@"\n' "$want" "$C/to_ok" "$REAL_TIMEOUT" > "$C/bin/timeout"
  chmod +x "$C/bin/timeout"
  # the shim only records calls whose limit argument equals the expected one; the stub shipper exits at once
  STUB_RC=0 STUB_SHIP=ok; export STUB_RC STUB_SHIP
  PATH="$C/bin:$ORIG_PATH"; COWLIC_SHIP_TIMEOUT="$val"; export COWLIC_SHIP_TIMEOUT; run
  PATH="$ORIG_PATH"; unset COWLIC_SHIP_TIMEOUT
  assert "ship timeout '$val' gives limit $want" "$([ -s "$C/to_ok" ] && grep -q ship_grafana.py "$C/to_ok" && echo 0 || echo 1)"
done
unset STUB_SHIP

# wrapper failures before ingest also go through finish: still exit 78 whatever the shipper does
new_case ship_novenv; rm -rf "$R/ingest/.venv"; STUB_RC=0; export STUB_RC; run
assert "ship: missing venv still exits 78 and ships nothing" "$([ "$rc" = 78 ] && [ ! -e "$STUB_SHIP_ARGS_FILE" ] && echo 0 || echo 1)"
new_case ship_nofile; rm -f "$R/ingest/ship_grafana.py"; STUB_SHIP=fail; export STUB_SHIP; run
assert "ship: missing shipper file is skipped silently" "$([ "$rc" = 0 ] && [ ! -e "$STUB_SHIP_ARGS_FILE" ] && ! grep -q 'WARN grafana' "$(LOGF)" && echo 0 || echo 1)"
new_case ship_lock; STUB_SLEEP=2; export STUB_SLEEP
( XDG_STATE_HOME="$S" bash "$R/scripts/run-ingest-local.sh" > "$C/out1" 2>&1 ) &
bg=$!
for _ in $(seq 50); do [ -s "$STUB_ARGS_FILE" ] && break; sleep 0.1; done
STUB_SHIP=ok; export STUB_SHIP; rm -f "$STUB_SHIP_ARGS_FILE"; run
assert "ship: a lock-blocked invocation ships nothing" "$([ "$rc" = 75 ] && [ ! -e "$STUB_SHIP_ARGS_FILE" ] && echo 0 || echo 1)"
wait "$bg" || true
unset STUB_SLEEP STUB_SHIP

# COWLIC_INGEST_ARGS: allowlisted ingest options forwarded by the wrapper
STUB_RC=0; unset STUB_SHIP STUB_RESULT; export STUB_RC
base_args() { echo "-u $R/ingest/ingest.py --sources $R/sources.yml --result-json $S/cowlic/.ingest-result.json"; }
new_case ia_unset; unset COWLIC_INGEST_ARGS; run
assert "ingest args unset: command line is exactly today's" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS_FILE")" = "$(base_args)" ] && echo 0 || echo 1)"
for blank in "" "   " "  "; do
  new_case ia_blank; COWLIC_INGEST_ARGS="$blank"; export COWLIC_INGEST_ARGS; run
  assert "ingest args blank '$blank': same as unset" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS_FILE")" = "$(base_args)" ] && echo 0 || echo 1)"
done
new_case ia_valid; COWLIC_INGEST_ARGS="--max-new 200 --max-minutes 90  --pace-seconds 7"; export COWLIC_INGEST_ARGS; run
assert "ingest args valid: appended after the fixed arguments" "$([ "$rc" = 0 ] && [ "$(cat "$STUB_ARGS_FILE")" = "$(base_args) --max-new 200 --max-minutes 90 --pace-seconds 7" ] && echo 0 || echo 1)"
assert "ingest args valid: option names logged" "$(grep -q 'extra ingest options from COWLIC_INGEST_ARGS: --max-new --max-minutes --pace-seconds' "$(LOGF)" && echo 0 || echo 1)"
new_case ia_one; COWLIC_INGEST_ARGS="--max-new 1"; export COWLIC_INGEST_ARGS; run
assert "ingest args valid: single option" "$([ "$rc" = 0 ] && grep -q -- '--result-json .* --max-new 1$' "$STUB_ARGS_FILE" && echo 0 || echo 1)"

# every value below must be rejected: exit 78, ingest never started, last-run.json class config, no value in output
ia_reject() { # name, value
  new_case "ia_$1"; COWLIC_INGEST_ARGS="$2"; export COWLIC_INGEST_ARGS; run
  assert "ingest args reject '$1': exit 78" "$([ "$rc" = 78 ] && echo 0 || echo 1)"
  assert "ingest args reject '$1': ingest not started" "$([ ! -e "$STUB_ARGS_FILE" ] && echo 0 || echo 1)"
  assert "ingest args reject '$1': clear log line" "$(grep -q 'COWLIC_INGEST_ARGS rejected' "$(LOGF)" && echo 0 || echo 1)"
  assert "ingest args reject '$1': no side effect file" "$([ ! -e "$C/pwned" ] && [ ! -e "$R/pwned" ] && [ ! -e "$PWD/pwned" ] && echo 0 || echo 1)"
  assert "ingest args reject '$1': value not in log or output" "$(grep -qE 'SENTINEL|pwned|bogus|rm |echo' "$(LOGF)" "$C/out" && echo 1 || echo 0)"
}
ia_reject unknown "--max-new 5 --bogus-SENTINEL 3"
ia_reject unknown-ingest-option "--state-file /tmp/SENTINEL"
# an unknown option with a perfectly valid integer passes the character-set check: only the allowlist stops it
ia_reject unknown-option-integer "--retry-attempts 3"
assert "ingest args reject unknown-option-integer: stopped by the allowlist, not the character set" "$(grep -q 'rejected: unknown option' "$(LOGF)" && echo 0 || echo 1)"
ia_reject max-artists "--max-artists 5"
ia_reject dry-run "--dry-run"
ia_reject bare-word "SENTINEL"
ia_reject positional "--max-new 5 https://open.spotify.com/playlist/SENTINEL"
ia_reject equals-form "--max-new=5"
ia_reject missing-value "--max-new"
ia_reject value-is-option "--max-new --max-minutes"
ia_reject non-integer "--max-new abc"
ia_reject float "--max-minutes 1.5"
ia_reject negative "--max-new -5"
ia_reject zero "--max-new 0"
ia_reject leading-zero "--max-new 007"
ia_reject too-big "--max-new 1000001"
ia_reject too-long-digits "--max-new 99999999999999999999"
ia_reject minutes-range "--max-minutes 1441"
ia_reject pace-range "--pace-seconds 3601"
ia_reject duplicate "--max-new 5 --max-new 6"
ia_reject semicolon "--max-new 5; touch $TMP/pwned"
ia_reject command-subst "--max-new \$(touch $TMP/pwned)"
ia_reject backticks "--max-new \`touch $TMP/pwned\`"
ia_reject dollar-var "--max-new \$HOME"
ia_reject pipe "--max-new 5 | tee $TMP/pwned"
ia_reject amp "--max-new 5 & touch $TMP/pwned"
ia_reject redirect "--max-new 5 > $TMP/pwned"
ia_reject glob "--max-new *"
ia_reject quotes "--max-new '5'"
ia_reject dquotes '--max-new "5"'
ia_reject backslash '--max-new 5\'
ia_reject newline "$(printf -- '--max-new 5\n--max-minutes 90')"
ia_reject newline-injection "$(printf -- '--max-new 5\ntouch %s/pwned' "$TMP")"
ia_reject tab "$(printf -- '--max-new\t5')"
ia_reject trailing-newline "$(printf -- '--max-new 5\n_')"
ia_reject over-200-chars "$(printf -- '--max-new 5 %0190d' 0)"
ia_reject nul-lookalike "--max-new 5%00"
ia_reject unicode "--max-new 5 --max-minutes ５"
[ ! -e "$TMP/pwned" ]; assert "no injection attempt created a file" "$?"
# a rejected run still writes last-run.json with class config and exit 78
new_case ia_lastrun; COWLIC_INGEST_ARGS="--bogus"; export COWLIC_INGEST_ARGS; run
assert "ingest args rejected: last-run exit 78 class config" "$([ "$(jget exit_code)" = 78 ] && [ "$(jget failure_class)" = config ] && echo 0 || echo 1)"
unset COWLIC_INGEST_ARGS

if ((failures)); then echo "$failures check(s) failed"; exit 1; fi
echo "All checks passed."

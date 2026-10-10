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
  export STUB_ARGS_FILE="$C/args"
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

new_case lr_signin; STUB_RC=3; STUB_RESULT="$(res_with youtube-sign-in)"; export STUB_RC STUB_RESULT; run
assert "last-run: youtube-sign-in keeps counts" "$([ "$(jget failure_class)" = youtube-sign-in ] && [ "$(jget exit_code)" = 3 ] && [ "$(jget result.counts.added)" = 2 ] && echo 0 || echo 1)"

new_case lr_crash; STUB_RC=1; STUB_RESULT=""; export STUB_RC STUB_RESULT
mkdir -p "$S/cowlic"; printf '%s\n' "$(res_with partial)" > "$S/cowlic/.ingest-result.json"  # stale file of an earlier run
run
assert "last-run: crash (no result), stale result not used" "$([ "$(jget failure_class)" = crash ] && [ "$(jget result)" = null ] && echo 0 || echo 1)"

new_case lr_garbage; STUB_RC=1; STUB_RESULT="not json, class partial"; export STUB_RC STUB_RESULT; run
assert "last-run: garbage result is dropped, still valid JSON" "$(valid_json && [ "$(jget result)" = null ] && [ "$(jget failure_class)" = crash ] && echo 0 || echo 1)"

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

if ((failures)); then echo "$failures check(s) failed"; exit 1; fi
echo "All checks passed."

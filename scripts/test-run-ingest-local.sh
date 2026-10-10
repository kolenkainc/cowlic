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
  printf '#!/bin/sh\n[ -z "${STUB_PREREQ_FAIL:-}" ] || { echo "MISSING  stub: fix it"; exit 1; }\necho "All prerequisites present."\n' > "$R/scripts/check-local-prereqs.sh"
  # Stub python: records its args, prints a line, optionally sleeps, exits with STUB_RC.
  cat > "$R/ingest/.venv/bin/python" <<'PY'
#!/bin/sh
echo "$@" > "$STUB_ARGS_FILE"
echo "stub ingest output"
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

if ((failures)); then echo "$failures check(s) failed"; exit 1; fi
echo "All checks passed."

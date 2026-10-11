#!/usr/bin/env bash
# Test for run-bootstrap-scheduled.sh on a fake repo copy in a temp dir with a STUB wrapper (it plays
# scripts/run-bootstrap-local.sh: records its arguments, writes bootstrap-last-run.json, exits with STUB_RC). The clock is
# COWLIC_BOOTSTRAP_NOW_EPOCH. Never runs the real wrapper or bootstrap, never touches the network, never touches
# ~/.config/systemd/user. Last section: mutation checks (a rule is broken in a copy of the launcher and the scenario must
# notice).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failures=0
assert() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi; }
is() { [ "$1" = "$2" ] && echo 0 || echo 1; }
has() { grep -q -- "$1" "$2" 2> /dev/null && echo 0 || echo 1; }
hasnt() { grep -q -- "$1" "$2" 2> /dev/null && echo 1 || echo 0; }
ok() { [ "$1" = "$2" ] && echo 0 || echo 1; }

export TZ=UTC
epoch() { date -d "$1" +%s; }  # uses the TZ in force
SECRET="sentinel_lfm_user"
LAUNCHER_SRC="$ROOT/scripts/run-bootstrap-scheduled.sh"

new_case() { # NAME [launcher source]: fake repo with the launcher and a stub wrapper
  C="$TMP/$1"; R="$C/root"; S="$C/state"
  mkdir -p "$R/scripts" "$S"
  cp "${2:-$LAUNCHER_SRC}" "$R/scripts/run-bootstrap-scheduled.sh"
  cat > "$R/scripts/run-bootstrap-local.sh" <<'SH'
#!/bin/sh
echo "$*" >> "$STUB_CALLS"
[ -z "${STUB_NO_SESSION:-}" ] || exit "${STUB_RC:-0}"   # 75 and the guard 78: nothing touched
[ -z "${STUB_RO:-}" ] || { mkdir -p "$XDG_STATE_HOME/cowlic"; chmod 500 "$XDG_STATE_HOME/cowlic"; exit "${STUB_RC:-0}"; }  # state dir becomes read-only
d="$XDG_STATE_HOME/cowlic"; mkdir -p "$d"
echo "stub wrapper line"
res="{\"stop_reason\": \"${STUB_STOP:-cap}\", \"counts\": {\"added\": ${STUB_ADDED:-0}}}"
[ -z "${STUB_RESULT:-}" ] || res="$STUB_RESULT"
printf '{"exit_code": %s, "result": %s}\n' "${STUB_RC:-0}" "$res" > "$d/bootstrap-last-run.json"
exit "${STUB_RC:-0}"
SH
  chmod +x "$R/scripts/"*.sh
  export STUB_CALLS="$C/calls"
  : > "$STUB_CALLS"
  unset STUB_RC STUB_STOP STUB_ADDED STUB_NO_SESSION STUB_RESULT || true
}
run() { rc=0; XDG_STATE_HOME="$S" COWLIC_EXTRA_SECRET="$SECRET" bash "$R/scripts/run-bootstrap-scheduled.sh" > "$C/out" 2>&1 || rc=$?; }
st() { echo "$S/cowlic/$1"; }
calls() { wc -l < "$STUB_CALLS"; }
call_args() { sed -n "${1:-1}p" "$STUB_CALLS"; }
at() { COWLIC_BOOTSTRAP_NOW_EPOCH="$(epoch "$1")"; export COWLIC_BOOTSTRAP_NOW_EPOCH; }

# 1. time-fit arithmetic (UTC): the wrapper refuses when now + minutes*60 + 660 >= next 21:10
new_case noon; at '2026-10-11 12:00:00'; run
assert "noon: exit 0, one call" "$([ "$rc" = 0 ] && [ "$(calls)" = 1 ] && echo 0 || echo 1)"
assert "noon: capped at 100 minutes and 400 tracks" "$(ok "$(call_args)" 'run --max-new 400 --max-minutes 100')"
new_case eight; at '2026-10-11 20:00:00'; run   # 4200 s to 21:10: (4200-661)/60 = 58
assert "20:00: 58 minutes, 232 tracks" "$(ok "$(call_args)" 'run --max-new 232 --max-minutes 58')"
new_case edge_ok; at '2026-10-11 20:43:59'; run  # 1561 s: (1561-661)/60 = 15 exactly
assert "20:43:59: 15 minutes fit, the session starts" "$(ok "$(call_args)" 'run --max-new 60 --max-minutes 15')"
new_case edge_skip; at '2026-10-11 20:44:00'; run  # 1560 s: 14 minutes
assert "20:44:00: 14 minutes fit, skip with exit 0 and no call" "$([ "$rc" = 0 ] && [ "$(calls)" = 0 ] && echo 0 || echo 1)"
assert "skip logs one line" "$([ "$(grep -c 'skip: only 14 minute' "$C/out")" = 1 ] && echo 0 || echo 1)"
new_case late; at '2026-10-11 20:55:00'; run
assert "20:55: skip" "$([ "$rc" = 0 ] && [ "$(calls)" = 0 ] && echo 0 || echo 1)"
new_case after; at '2026-10-11 21:10:30'; run   # within 60 s after 21:10 that day's 21:10 still counts
assert "21:10:30: skip (that day's 21:10 counts)" "$([ "$rc" = 0 ] && [ "$(calls)" = 0 ] && echo 0 || echo 1)"
new_case next_day; at '2026-10-11 21:11:30'; run
assert "21:11:30: the next 21:10 is tomorrow, 100 minutes" "$(ok "$(call_args)" 'run --max-new 400 --max-minutes 100')"
new_case midnight; at '2026-10-11 23:59:59'; run
assert "23:59:59 (day boundary): 100 minutes" "$(ok "$(call_args)" 'run --max-new 400 --max-minutes 100')"
new_case early; at '2026-10-12 00:00:00'; run
assert "00:00 (new day): 100 minutes" "$(ok "$(call_args)" 'run --max-new 400 --max-minutes 100')"
# DST: Europe/Berlin falls back on 2026-10-25 and springs forward on 2026-03-29 (both at night); 21:10 stays local time
export TZ=Europe/Berlin
new_case dst_fall; at '2026-10-25 20:00:00'; run
assert "DST day (autumn) 20:00 local: 58 minutes" "$(ok "$(call_args)" 'run --max-new 232 --max-minutes 58')"
new_case dst_fall_night; at '2026-10-25 01:30:00'; run
assert "DST night (autumn) 01:30 local: capped at 100" "$(ok "$(call_args)" 'run --max-new 400 --max-minutes 100')"
new_case dst_spring; at '2026-03-29 20:00:00'; run
assert "DST day (spring) 20:00 local: 58 minutes" "$(ok "$(call_args)" 'run --max-new 232 --max-minutes 58')"
export TZ=UTC
new_case badclock; COWLIC_BOOTSTRAP_NOW_EPOCH='12x' run
assert "bad test clock: exit 78, no call" "$([ "$rc" = 78 ] && [ "$(calls)" = 0 ] && echo 0 || echo 1)"
at '2026-10-11 12:00:00'

# 2. breaker after exit 3
new_case halt3; STUB_RC=3 STUB_STOP=sign-in; export STUB_RC STUB_STOP; run
assert "exit 3: one call only (no retry), exit 1" "$([ "$(calls)" = 1 ] && [ "$rc" = 1 ] && echo 0 || echo 1)"
assert "exit 3: halt marker, mode 0600" "$([ "$(stat -c %a "$(st bootstrap-halt)")" = 600 ] && echo 0 || echo 1)"
assert "exit 3: logged" "$(has 'halted after a YouTube sign-in challenge' "$C/out")"
unset STUB_RC STUB_STOP
run
assert "halted: next scheduled run exits 0 without calling the wrapper" "$([ "$rc" = 0 ] && [ "$(calls)" = 1 ] && echo 0 || echo 1)"
assert "halted: says so" "$(has 'halted (marker' "$C/out")"
rm -f "$(st bootstrap-halt)"; STUB_ADDED=2; export STUB_ADDED; run; unset STUB_ADDED
assert "marker removed by the owner: sessions resume" "$([ "$(calls)" = 2 ] && echo 0 || echo 1)"

# 3. consecutive failures
new_case fails; STUB_RC=1 STUB_ADDED=0; export STUB_RC STUB_ADDED
run; run
assert "2 failed sessions: no halt, counter 2" "$([ ! -e "$(st bootstrap-halt)" ] && [ "$(cat "$(st bootstrap-fails)")" = 2 ] && echo 0 || echo 1)"
run
assert "3rd failed session: halt marker (0600), exit 1" "$([ "$rc" = 1 ] && [ "$(stat -c %a "$(st bootstrap-halt)")" = 600 ] && echo 0 || echo 1)"
run
assert "then halted: 3 calls only" "$(ok "$(calls)" 3)"
new_case fails_reset; STUB_RC=1 STUB_ADDED=0; export STUB_RC STUB_ADDED
run; run; STUB_ADDED=5; run
assert "failed but added tracks: counter reset, exit 0" "$([ "$rc" = 0 ] && [ ! -e "$(st bootstrap-fails)" ] && echo 0 || echo 1)"
STUB_ADDED=0; run
assert "after the reset a failure counts from 1" "$([ "$(cat "$(st bootstrap-fails)")" = 1 ] && [ ! -e "$(st bootstrap-halt)" ] && echo 0 || echo 1)"
new_case fails_ok; STUB_RC=1 STUB_ADDED=0; export STUB_RC STUB_ADDED; run; run; STUB_RC=0 STUB_ADDED=1 run; unset STUB_RC STUB_ADDED
assert "an ok session resets the counter" "$([ ! -e "$(st bootstrap-fails)" ] && echo 0 || echo 1)"
new_case fails_cfg; STUB_RC=1 STUB_ADDED=0; export STUB_RC STUB_ADDED; COWLIC_BOOTSTRAP_MAX_FAILS=1 run
assert "COWLIC_BOOTSTRAP_MAX_FAILS=1: first failure halts" "$([ -e "$(st bootstrap-halt)" ] && echo 0 || echo 1)"
new_case nolastrun; STUB_RC=1 STUB_RESULT=null; export STUB_RC STUB_RESULT; run
assert "result null: counts as added 0 (counter 1)" "$([ "$(cat "$(st bootstrap-fails)")" = 1 ] && echo 0 || echo 1)"
unset STUB_RC STUB_ADDED STUB_RESULT

# 4. exit 75 and exit 78
new_case locked; STUB_RC=75 STUB_NO_SESSION=1; export STUB_RC STUB_NO_SESSION; run; run; run; run
assert "exit 75: quiet skip, exit 0, never counted or halted" "$([ "$rc" = 0 ] && [ ! -e "$(st bootstrap-fails)" ] && [ ! -e "$(st bootstrap-halt)" ] && echo 0 || echo 1)"
new_case guard78; at '2026-10-11 20:43:59'; STUB_RC=78 STUB_NO_SESSION=1; export STUB_RC STUB_NO_SESSION; run
assert "exit 78 while the time still fits (prerequisite problem): a failure, counted" "$([ "$(cat "$(st bootstrap-fails)")" = 1 ] && echo 0 || echo 1)"
unset STUB_RC STUB_NO_SESSION
seeded_78() { # NAME LAUNCHER: a PRE-SEEDED last-run (added 232, older than the launcher start) and a wrapper that exits 78 early
  new_case "$1" "$2"; at '2026-10-11 12:00:00'
  mkdir -p "$S/cowlic"
  printf '{"exit_code": 0, "result": {"stop_reason": "cap", "counts": {"added": 232}}}\n' > "$S/cowlic/bootstrap-last-run.json"
  touch -d '2 hours ago' "$S/cowlic/bootstrap-last-run.json"
  STUB_RC=78 STUB_NO_SESSION=1 run; unset STUB_RC STUB_NO_SESSION
  [ "$(cat "$(st bootstrap-fails)" 2> /dev/null)" = 1 ]
}
r=0; seeded_78 stale78 "$LAUNCHER_SRC" || r=1
assert "early wrapper exit 78 with a stale last-run (added 232): counted as a failure" "$r"
assert "stale last-run is not 'tracks were added'" "$(hasnt 'track(s) were added' "$C/out")"
new_case stale1; mkdir -p "$S/cowlic"
printf '{"exit_code": 0, "result": {"stop_reason": "cap", "counts": {"added": 232}}}\n' > "$S/cowlic/bootstrap-last-run.json"
touch -d '2 hours ago' "$S/cowlic/bootstrap-last-run.json"
STUB_RC=1 STUB_NO_SESSION=1 run; unset STUB_RC STUB_NO_SESSION
assert "any exit without a fresh last-run ignores the old file (exit 1, counter 1)" "$([ "$(cat "$(st bootstrap-fails)")" = 1 ] && echo 0 || echo 1)"
# a halt marker that cannot be written: the breaker is NOT armed, say so loudly and exit non-zero
new_case rohalt; STUB_RC=3 STUB_RO=1; export STUB_RC STUB_RO; run; unset STUB_RC STUB_RO
chmod 700 "$S/cowlic"
assert "halt marker unwritable after exit 3: exit 1 and a loud 'NOT armed' line" "$([ "$rc" = 1 ] && grep -q 'breaker is NOT armed' "$C/out" && echo 0 || echo 1)"
# the clock passes the fit while the wrapper runs: a `date` shim answers +%s from a file the stub wrapper advances
new_case guard78b; unset COWLIC_BOOTSTRAP_NOW_EPOCH
mkdir -p "$C/bin"
cat > "$C/bin/date" <<SH
#!/bin/sh
if [ "\$*" = "+%s" ]; then cat "$C/clock"; else exec /bin/date "\$@"; fi
SH
cat > "$R/scripts/run-bootstrap-local.sh" <<SH
#!/bin/sh
echo "\$*" >> "\$STUB_CALLS"
printf '%s' "$(epoch '2026-10-11 20:45:00')" > "$C/clock"
exit 78
SH
chmod +x "$C/bin/date" "$R/scripts/run-bootstrap-local.sh"
printf '%s' "$(epoch '2026-10-11 20:43:59')" > "$C/clock"
rc=0; PATH="$C/bin:$PATH" XDG_STATE_HOME="$S" bash "$R/scripts/run-bootstrap-scheduled.sh" > "$C/out" 2>&1 || rc=$?
assert "exit 78 because the time no longer fits: quiet skip, not counted" "$([ "$rc" = 0 ] && [ ! -e "$(st bootstrap-fails)" ] && [ ! -e "$(st bootstrap-halt)" ] && [ "$(calls)" = 1 ] && echo 0 || echo 1)"
assert "that skip is logged" "$(has 'no longer fits' "$C/out")"
at '2026-10-11 12:00:00'

# 5. done detection
new_case done; STUB_RC=0 STUB_STOP=done STUB_ADDED=0; export STUB_RC STUB_STOP STUB_ADDED; run
assert "done: exit 0, marker (0600), logged" "$([ "$rc" = 0 ] && [ "$(stat -c %a "$(st bootstrap-done)")" = 600 ] && echo 0 || echo 1)"
assert "done: names the command to disable the timer" "$(has 'disable --now cowlic-bootstrap.timer' "$C/out")"
run
assert "done: later runs do not call the wrapper and are quiet-ish (exit 0)" "$([ "$rc" = 0 ] && [ "$(calls)" = 1 ] && echo 0 || echo 1)"
new_case notdone; STUB_RC=0 STUB_STOP=cap STUB_ADDED=9; export STUB_RC STUB_STOP STUB_ADDED; run
assert "cap stop: no done marker" "$([ ! -e "$(st bootstrap-done)" ] && echo 0 || echo 1)"
new_case failed_done; STUB_RC=1 STUB_STOP=done STUB_ADDED=0; export STUB_RC STUB_STOP STUB_ADDED; run
assert "done only counts with exit 0" "$([ ! -e "$(st bootstrap-done)" ] && echo 0 || echo 1)"
unset STUB_RC STUB_STOP STUB_ADDED

# 6. no secrets, no user name; the arguments the launcher passes are only the two caps
new_case quiet; STUB_RC=3; export STUB_RC; run; unset STUB_RC
assert "no secret in the output" "$(hasnt "$SECRET" "$C/out")"
assert "no secret in any state file" "$([ -z "$(grep -rl "$SECRET" "$S" 2> /dev/null || true)" ] && echo 0 || echo 1)"
assert "the wrapper gets exactly: run --max-new N --max-minutes M" "$([[ "$(call_args)" =~ ^run\ --max-new\ [0-9]+\ --max-minutes\ [0-9]+$ ]] && echo 0 || echo 1)"
assert "state dir mode 0700" "$([ "$(stat -c %a "$S/cowlic")" = 700 ] && echo 0 || echo 1)"
assert "launcher never reads .env or traces" "$([ "$(grep -c '\.env\|set -x' "$LAUNCHER_SRC" | head -n1)" -le 2 ] && [ "$(grep -c '^ *\(source\|\.\) .*env' "$LAUNCHER_SRC")" = 0 ] && echo 0 || echo 1)"

# 7. mutation checks: each rule is broken in a copy of the launcher; the scenario must then FAIL
scenario_breaker() { # 0 when the launcher behaves correctly after exit 3
  new_case "$1" "$2"; STUB_RC=3 STUB_STOP=sign-in; export STUB_RC STUB_STOP; run; unset STUB_RC STUB_STOP
  [ -e "$(st bootstrap-halt)" ] && [ "$(calls)" = 1 ]
}
scenario_fit() { # 0 when 15 minutes fit exactly at 20:43:59 and 14 do not at 20:44:00
  new_case "$1a" "$2"; at '2026-10-11 20:43:59'; run
  local a=0 b=0
  [ "$(calls)" = 1 ] || a=1
  new_case "$1b" "$2"; at '2026-10-11 20:44:00'; run
  [ "$(calls)" = 0 ] || b=1
  at '2026-10-11 12:00:00'
  [ "$a" = 0 ] && [ "$b" = 0 ]
}
scenario_fails() { # 0 when the third failed session halts
  new_case "$1" "$2"; STUB_RC=1 STUB_ADDED=0; export STUB_RC STUB_ADDED; run; run; run; unset STUB_RC STUB_ADDED
  [ -e "$(st bootstrap-halt)" ]
}
mutant() { # NAME SED-EXPRESSION
  sed "$2" "$LAUNCHER_SRC" > "$TMP/mutant-$1.sh"
  if cmp -s "$LAUNCHER_SRC" "$TMP/mutant-$1.sh"; then echo "FAIL mutation $1: the sed expression changed nothing"; failures=$((failures + 1)); return; fi
}
assert "mutation baseline: the scenarios pass on the real launcher" "$({ scenario_breaker mb0 "$LAUNCHER_SRC" && scenario_fit mf0 "$LAUNCHER_SRC" && scenario_fails mx0 "$LAUNCHER_SRC"; } && echo 0 || echo 1)"
mutant nobreaker 's|"\$EXIT_SIGN_IN")|"99")|'
assert "mutation: breaker removed is caught" "$(scenario_breaker m1 "$TMP/mutant-nobreaker.sh" && echo 1 || echo 0)"
mutant margin 's|next - now - 661|next - now - 601|'
assert "mutation: fit arithmetic off by the margin is caught" "$(scenario_fit m2 "$TMP/mutant-margin.sh" && echo 1 || echo 0)"
mutant loop 's|^rc=\$?$|rc=$?; if [ "$rc" = 3 ]; then "$WRAPPER" run --max-new "$NEW" --max-minutes "$MINUTES"; rc=$?; fi|'
assert "mutation: retry on exit 3 is caught" "$(scenario_breaker m3 "$TMP/mutant-loop.sh" && echo 1 || echo 0)"
mutant nofailhalt 's|\[ "\$n" -ge "\$MAX_FAILS" \]|[ "$n" -ge 999 ]|'
assert "mutation: failure halt removed is caught" "$(scenario_fails m4 "$TMP/mutant-nofailhalt.sh" && echo 1 || echo 0)"

mutant nofresh 's|! \[ "\$MARK" -nt "\$LAST_RUN" \] \&\& ||'
seeded_rc1() { # same with a non-78 exit: only the freshness check protects this one
  new_case "$1" "$2"; mkdir -p "$S/cowlic"
  printf '{"exit_code": 0, "result": {"stop_reason": "cap", "counts": {"added": 232}}}\n' > "$S/cowlic/bootstrap-last-run.json"
  touch -d '2 hours ago' "$S/cowlic/bootstrap-last-run.json"
  STUB_RC=1 STUB_NO_SESSION=1 run; unset STUB_RC STUB_NO_SESSION
  [ "$(cat "$(st bootstrap-fails)" 2> /dev/null)" = 1 ]
}
assert "mutation baseline: seeded rc 1 scenario passes on the real launcher" "$(seeded_rc1 m5a "$LAUNCHER_SRC" && echo 0 || echo 1)"
assert "mutation: stale last-run accepted is caught" "$(seeded_rc1 m5 "$TMP/mutant-nofresh.sh" && echo 1 || echo 0)"
mutant no78 's|^\[ "\$rc" != "\$EXIT_PREREQ" \] \|\| ADDED=0.*$|:|'
assert "mutation: 78 not forced to added 0 (with the freshness check also off) is caught" "$(sed 's|! \[ "\$MARK" -nt "\$LAST_RUN" \] \&\& ||' "$TMP/mutant-no78.sh" > "$TMP/mutant-no78b.sh"; seeded_78 m6 "$TMP/mutant-no78b.sh" && echo 1 || echo 0)"
mutant swallow 's|if ! write_marker "\$HALT" "\$(now_utc) exit 3: YouTube sign-in challenge"; then|if false; then|'
assert "mutation: unwritable halt marker swallowed is caught" "$(new_case m7 "$TMP/mutant-swallow.sh"; STUB_RC=3 STUB_RO=1 run; chmod 700 "$S/cowlic"; grep -q 'breaker is NOT armed' "$C/out" && echo 1 || echo 0)"

echo
if [ "$failures" -eq 0 ]; then echo "all tests passed"; else echo "$failures test(s) FAILED"; exit 1; fi

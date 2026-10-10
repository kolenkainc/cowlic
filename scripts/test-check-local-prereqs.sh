#!/usr/bin/env bash
# Test for check-local-prereqs.sh with stub binaries on a restricted PATH. No network, no real tools used.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/check-local-prereqs.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

BASH_BIN="$(command -v bash)"
failures=0
assert() { # name, condition result
  if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi
}

# new_case NAME: fresh fake root + bin with only the tools the script needs (grep, head via symlinks).
new_case() {
  C="$TMP/$1"; mkdir -p "$C/bin" "$C/root/ingest/.venv/bin"
  for t in grep head timeout; do ln -s "$(command -v "$t")" "$C/bin/$t"; done
  for t in ffmpeg flock; do printf '#!/bin/sh\nexit 0\n' > "$C/bin/$t"; chmod +x "$C/bin/$t"; done
  set_deno "$1" 2.9.5
  # Venv python stub: succeeds for every -c check unless the module name is in STUB_BAD_MOD.
  cat > "$C/root/ingest/.venv/bin/python" <<'PY'
#!/bin/sh
case "$2" in *version_info*) [ -z "${STUB_OLD_PY:-}" ] || exit 1 ;; esac
case "$2" in *"import ${STUB_BAD_MOD:-@@}"*) exit 1 ;; esac
exit 0
PY
  chmod +x "$C/root/ingest/.venv/bin/python"
  cat > "$C/root/.env" <<'ENV'
TG_BOT_TOKEN=x
TG_CHANNEL_ID=x
SPOTIFY_CLIENT_ID=x
SPOTIFY_CLIENT_SECRET=x
CLOUDFLARE_API_TOKEN=x
CLOUDFLARE_ACCOUNT_ID=x
D1_DATABASE_ID=x
ENV
}
set_deno() { printf '#!/bin/sh\necho "deno %s (stable, release, x86_64-unknown-linux-gnu)"\n' "$2" > "$TMP/$1/bin/deno"; chmod +x "$TMP/$1/bin/deno"; }
run() { # case name; sets OUT and RC
  set +e
  OUT="$(PATH="$TMP/$1/bin" CHECK_REPO_ROOT="$TMP/$1/root" "$BASH_BIN" "$SCRIPT" 2>&1)"; RC=$?
  set -e
}

new_case all; run all
assert "all present: exit 0" "$([ "$RC" = 0 ]; echo $?)"
assert "all present: no MISSING" "$(! grep -q MISSING <<<"$OUT"; echo $?)"

new_case old; set_deno old 2.2.9; run old
assert "deno too old: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "deno too old: message" "$(grep -q 'MISSING  deno' <<<"$OUT"; echo $?)"

new_case v210; set_deno v210 2.10.0; run v210
assert "deno 2.10.0 accepted over 2.3.0" "$([ "$RC" = 0 ]; echo $?)"

new_case v23; set_deno v23 2.3.0; run v23
assert "deno 2.3.0 accepted (boundary)" "$([ "$RC" = 0 ]; echo $?)"

new_case noff; rm "$TMP/noff/bin/ffmpeg"; run noff
assert "ffmpeg missing: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "ffmpeg missing: message" "$(grep -q 'MISSING  ffmpeg' <<<"$OUT"; echo $?)"

new_case novenv; rm -rf "$TMP/novenv/root/ingest/.venv"; run novenv
assert "venv missing: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "venv missing: message" "$(grep -q 'MISSING  ingest/.venv' <<<"$OUT"; echo $?)"

new_case nomod
set +e; OUT="$(STUB_BAD_MOD=spotdl PATH="$TMP/nomod/bin" CHECK_REPO_ROOT="$TMP/nomod/root" "$BASH_BIN" "$SCRIPT" 2>&1)"; RC=$?; set -e
assert "spotdl not importable: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "spotdl not importable: message" "$(grep -q 'MISSING  python module spotdl' <<<"$OUT"; echo $?)"

new_case nokey; grep -v '^TG_CHANNEL_ID=' "$TMP/nokey/root/.env" > "$TMP/nokey/env.new"; mv "$TMP/nokey/env.new" "$TMP/nokey/root/.env"; run nokey
assert ".env key missing: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert ".env key missing: names the key" "$(grep -q 'TG_CHANNEL_ID' <<<"$OUT"; echo $?)"

new_case noenv; rm "$TMP/noenv/root/.env"; run noenv
assert ".env missing: exit 1" "$([ "$RC" = 1 ]; echo $?)"

# .env edge cases, mirroring tg_check.load_env + ingest.py (empty value = missing, export not accepted).
env_case() { # name, sed expression applied to .env
  new_case "$1"; sed -i "$2" "$TMP/$1/root/.env"; run "$1"
}
env_case empty 's/^TG_BOT_TOKEN=x/TG_BOT_TOKEN=/'
assert ".env empty value: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert ".env empty value: names key" "$(grep -q 'MISSING  .env.*TG_BOT_TOKEN' <<<"$OUT"; echo $?)"
env_case blank 's/^TG_BOT_TOKEN=x/TG_BOT_TOKEN=   /'
assert ".env blank value: exit 1" "$([ "$RC" = 1 ]; echo $?)"
env_case export 's/^TG_BOT_TOKEN=x/export TG_BOT_TOKEN=x/'
assert ".env export form: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert ".env export form: hint" "$(grep -q "export' is not supported" <<<"$OUT"; echo $?)"
env_case spaced 's/^TG_BOT_TOKEN=x/  TG_BOT_TOKEN = x/'
assert ".env leading space and spaced = accepted" "$([ "$RC" = 0 ]; echo $?)"
env_case commented 's/^TG_BOT_TOKEN=x/#TG_BOT_TOKEN=x/'
assert ".env commented key: exit 1" "$([ "$RC" = 1 ]; echo $?)"
env_case indcomm 's/^TG_BOT_TOKEN=x/  # TG_BOT_TOKEN=x/'
assert ".env indented comment: exit 1" "$([ "$RC" = 1 ]; echo $?)"
env_case prefix 's/^TG_BOT_TOKEN=x/XTG_BOT_TOKEN=x/'
assert ".env prefix match not counted: exit 1" "$([ "$RC" = 1 ]; echo $?)"
env_case firstwins 's/^TG_BOT_TOKEN=x/TG_BOT_TOKEN=\nTG_BOT_TOKEN=x/'
assert ".env first definition wins (empty then set): exit 1" "$([ "$RC" = 1 ]; echo $?)"

# Version and python edge cases.
new_case rc; set_deno rc 2.4.0-rc.1; run rc
assert "deno 2.4.0-rc.1 accepted" "$([ "$RC" = 0 ]; echo $?)"
new_case oldpy; run_old() { set +e; OUT="$(STUB_OLD_PY=1 PATH="$TMP/oldpy/bin" CHECK_REPO_ROOT="$TMP/oldpy/root" "$BASH_BIN" "$SCRIPT" 2>&1)"; RC=$?; set -e; }; run_old
assert "python < 3.10 (venv): exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "python < 3.10 (venv): message" "$(grep -q 'MISSING  python:' <<<"$OUT"; echo $?)"
new_case fb; rm -rf "$TMP/fb/root/ingest/.venv"; printf '#!/bin/sh\nexit 0\n' > "$TMP/fb/bin/python3"; chmod +x "$TMP/fb/bin/python3"; run fb
assert "no venv, python3 present: only venv MISSING" "$([ "$(grep -c MISSING <<<"$OUT")" = 1 ]; echo $?)"
new_case fbold; rm -rf "$TMP/fbold/root/ingest/.venv"; printf '#!/bin/sh\nexit 1\n' > "$TMP/fbold/bin/python3"; chmod +x "$TMP/fbold/bin/python3"; run fbold
assert "no venv, python3 too old: reported" "$(grep -q 'MISSING  python3' <<<"$OUT"; echo $?)"
new_case nopy; rm -rf "$TMP/nopy/root/ingest/.venv"; run nopy
assert "no venv, no python3: reported" "$(grep -q 'MISSING  python3: not on PATH' <<<"$OUT"; echo $?)"

# Drift: the script's copies must equal the ones in ingest/ingest.py.
req_list() { sed -n '/^REQUIRED_ENV *[=]/,/[])]/p' "$1" | sed 's/^REQUIRED_ENV *=//' | grep -o '[A-Z][A-Z0-9_]*' | tr '\n' ' '; }
[ -n "$(req_list "$ROOT/ingest/ingest.py")" ] || { echo "FAIL could not extract REQUIRED_ENV from ingest.py"; failures=$((failures + 1)); }
assert "REQUIRED_ENV matches ingest.py" "$([ "$(req_list "$ROOT/ingest/ingest.py")" = "$(req_list "$SCRIPT")" ]; echo $?)"
py_min="$(sed -n 's/^MIN_DENO = (\([0-9]*\), \([0-9]*\), \([0-9]*\)).*/\1.\2.\3/p' "$ROOT/ingest/ingest.py")"
sh_min="$(sed -n 's/^MIN_DENO="\([0-9.]*\)".*/\1/p' "$SCRIPT")"
assert "MIN_DENO matches ingest.py ($py_min)" "$([ -n "$py_min" ] && [ "$py_min" = "$sh_min" ]; echo $?)"

# Values must never be printed (also on the error path).
new_case errsecret; sed -i 's/^TG_CHANNEL_ID=x/TG_CHANNEL_ID=/; s/^TG_BOT_TOKEN=x/TG_BOT_TOKEN=SECRETVALUE123/; s/^D1_DATABASE_ID=x/export D1_DATABASE_ID=SECRETVALUE456/' "$TMP/errsecret/root/.env"; run errsecret
assert "error path: exit 1" "$([ "$RC" = 1 ]; echo $?)"
assert "error path: no .env value in output" "$(! grep -q SECRETVALUE <<<"$OUT"; echo $?)"

new_case secret; sed -i 's/^TG_BOT_TOKEN=x/TG_BOT_TOKEN=SECRETVALUE123/' "$TMP/secret/root/.env"; run secret
assert "no .env value in output" "$(! grep -q SECRETVALUE123 <<<"$OUT"; echo $?)"

[ "$failures" = 0 ] && echo "all passed" || { echo "$failures failed"; exit 1; }

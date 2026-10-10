#!/usr/bin/env bash
# Test for sync-secrets.sh: temp .env fixture + npx PATH shim. Never touches the network.
# Proves values reach the tools only via stdin and never appear in stdout/stderr (also under xtrace).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
# The script runs from a copy of the repo layout with a dummy worker/wrangler.local.toml (gitignored, so absent in a
# fresh clone), with the real manifest. FROOT is what the script sees as the repo root.
FROOT="$TMP/fake"
mkdir -p "$FROOT/scripts" "$FROOT/worker"
cp "$ROOT/scripts/sync-secrets.sh" "$ROOT/scripts/secrets.manifest" "$FROOT/scripts/"
: > "$FROOT/worker/wrangler.local.toml"
SCRIPT="$FROOT/scripts/sync-secrets.sh"

# Shim: logs argv (never stdin) to calls.log, cwd to pwd.log, stdin to stdin.log. FAIL_AT=N makes the
# Nth call exit 1 (after recording). The shim sets +x itself: it stands in for a non-bash tool.
# A gh shim is installed too: the script must never call gh any more.
cat > "$TMP/bin/npx" <<SHIM
#!/usr/bin/env bash
set +x
printf '%s\n' "npx \$*" >> "$TMP/calls.log"
printf '%s\n' "npx \$PWD" >> "$TMP/pwd.log"
printf '%s: %s\n' "npx \$*" "\$(cat)" >> "$TMP/stdin.log"
if [ -n "\${FAIL_AT:-}" ] && [ "\$(wc -l < "$TMP/calls.log")" -eq "\$FAIL_AT" ]; then echo "shim: simulated failure" >&2; exit 1; fi
SHIM
cat > "$TMP/bin/gh" <<SHIM
#!/usr/bin/env bash
echo "gh \$*" >> "$TMP/gh-called.log"
SHIM
chmod +x "$TMP/bin/npx" "$TMP/bin/gh"

# Ingest values live in the .env but must never be synced (ingest runs locally).
CANARIES=(CANARY_TG_9f3a CANARY_USER_7b21 CANARY_PASS_c0de CANARY_SPID_1111 CANARY_SPSEC_2222 CANARY_INGEST_3333 CANARY_CHAN_4444 CANARY_ACCT_5555 CANARY_D1_6666)
cat > "$TMP/env" <<ENVF
# comment
export TG_BOT_TOKEN=${CANARIES[0]}
SUBSONIC_USER="${CANARIES[1]}"
SUBSONIC_PASSWORD='${CANARIES[2]}'
SPOTIFY_CLIENT_ID=${CANARIES[3]}
SPOTIFY_CLIENT_SECRET=${CANARIES[4]}
INGEST_CLOUDFLARE_API_TOKEN=${CANARIES[5]}
TG_CHANNEL_ID=${CANARIES[6]}
CLOUDFLARE_ACCOUNT_ID=${CANARIES[7]}
D1_DATABASE_ID=${CANARIES[8]}
CLOUDFLARE_API_TOKEN=CANARY_BROAD_9999
ENVF
cp "$TMP/env" "$TMP/env.full"

fail() { echo "FAIL: $1" >&2; exit 1; }
run() { # run <outfile> args... ; env ENV_FILE=$TMP/env; returns script exit code
  local out="$1"; shift
  PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" "$SCRIPT" "$@" > "$out.out" 2> "$out.err" && return 0 || return $?
}
no_leak() { # no_leak <label> files...
  local label="$1"; shift
  local f
  for f in "$@"; do [ -f "$f" ] || fail "$label: file to check does not exist: $f"; done
  for c in "${CANARIES[@]}" CANARY_BROAD_9999; do
    if grep -q "$c" "$@"; then fail "$label: value $c leaked"; fi
  done
}

# 1. dry run: all present, nothing executed, no values shown
run "$TMP/t1" --dry-run || fail "dry-run exit"
[ ! -e "$TMP/calls.log" ] || fail "dry-run called tools"
grep -q 'TG_BOT_TOKEN.*worker-secret.*TG_BOT_TOKEN.*present' "$TMP/t1.out" || fail "dry-run output"
no_leak dry-run "$TMP/t1.out" "$TMP/t1.err"

# 2. real sync (shim): correct calls, values only on stdin, no leak even under xtrace
(cd "$TMP" && env PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" SHELLOPTS=xtrace "$SCRIPT") > "$TMP/t2.out" 2> "$TMP/t2.err" || fail "sync exit"
no_leak sync "$TMP/t2.out" "$TMP/t2.err" "$TMP/calls.log"
[ "$(wc -l < "$TMP/calls.log")" -eq 3 ] || fail "expected 3 tool calls (one per worker secret)"
[ ! -e "$TMP/gh-called.log" ] || fail "gh must not be called: nothing is synced to GitHub"
grep -qx 'npx --no-install wrangler secret put SUBSONIC_USER -c wrangler.local.toml: CANARY_USER_7b21' "$TMP/stdin.log" || fail "worker secret, quote stripping"
grep -qx 'npx --no-install wrangler secret put TG_BOT_TOKEN -c wrangler.local.toml: CANARY_TG_9f3a' "$TMP/stdin.log" || fail "export prefix"
grep -q 'CANARY_BROAD\|CANARY_SPID\|CANARY_D1' "$TMP/stdin.log" && fail "unmanifested key synced"
[ "$(grep -c '^npx ' "$TMP/pwd.log")" -eq 3 ] || fail "expected 3 npx calls"
[ "$(grep -cx "npx $FROOT/worker" "$TMP/pwd.log")" -eq 3 ] || fail "npx not run from worker/"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" bash -x "$SCRIPT" --only SUBSONIC_USER > /dev/null 2> "$TMP/t2x.err" || fail "bash -x exit"
no_leak "script resists xtrace via its own set +x" "$TMP/t2x.err"

# 3. --only syncs exactly one entry; a key outside the manifest (an ingest value) is refused
rm -f "$TMP/calls.log" "$TMP/stdin.log"
run "$TMP/t3" --only SUBSONIC_PASSWORD || fail "--only exit"
[ "$(wc -l < "$TMP/calls.log")" -eq 1 ] || fail "--only call count"
no_leak only "$TMP/t3.out" "$TMP/t3.err"
rm -f "$TMP/calls.log" "$TMP/stdin.log"
run "$TMP/t3b" --only SPOTIFY_CLIENT_ID && fail "ingest key must not be in the manifest"
[ ! -e "$TMP/calls.log" ] || fail "call made for an ingest key"

# 4. unknown --only key fails
run "$TMP/t4" --only NOPE && fail "unknown --only should fail"
grep -q 'not in the manifest' "$TMP/t4.err" || fail "unknown --only message"

# 5. missing key: dry-run shows missing and fails; real sync syncs nothing
grep -v '^SUBSONIC_PASSWORD' "$TMP/env.full" > "$TMP/env"
rm -f "$TMP/calls.log" "$TMP/stdin.log"
run "$TMP/t5" --dry-run && fail "missing key dry-run should fail"
grep -q 'SUBSONIC_PASSWORD.*missing' "$TMP/t5.out" || fail "missing not reported"
run "$TMP/t5b" && fail "missing key sync should fail"
[ ! -e "$TMP/calls.log" ] || fail "partial sync with missing key"
no_leak missing "$TMP/t5.out" "$TMP/t5.err" "$TMP/t5b.out" "$TMP/t5b.err"
cp "$TMP/env.full" "$TMP/env"

# 6. bad manifests rejected with a line number before any call: unknown target, removed GitHub targets
# (also with an environment suffix), @env on a worker target, duplicate destinations
for badm in 'TG_BOT_TOKEN gh-secrte' 'TG_BOT_TOKEN gh-env-secret' 'TG_BOT_TOKEN gh-env-secret@ingest' 'TG_BOT_TOKEN gh-secret' 'TG_BOT_TOKEN gh-variable' 'TG_BOT_TOKEN worker-secret@ingest' $'TG_BOT_TOKEN worker-secret\nTG_BOT_TOKEN worker-secret'; do
  printf '%s\n' "$badm" > "$TMP/bad.manifest"; rm -f "$TMP/calls.log"
  PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" MANIFEST="$TMP/bad.manifest" "$SCRIPT" > "$TMP/t6.out" 2> "$TMP/t6.err" && fail "bad manifest accepted: $badm"
  grep -q 'manifest line [12]' "$TMP/t6.err" || fail "manifest line not reported: $badm"
  [ ! -e "$TMP/calls.log" ] || fail "call made with bad manifest: $badm"
  no_leak badmanifest "$TMP/t6.out" "$TMP/t6.err"
done

# 6b. partial failure: 2nd of 3 calls fails
rm -f "$TMP/calls.log" "$TMP/stdin.log" "$TMP/pwd.log"
rc=0; FAIL_AT=2 run "$TMP/t7" || rc=$?
[ "$rc" -eq 1 ] || fail "partial failure exit code is $rc, expected 1"
[ "$(wc -l < "$TMP/calls.log")" -eq 2 ] || fail "script continued after a failed call"
grep -q 'failed at SUBSONIC_USER' "$TMP/t7.err" || fail "failing entry not named"
grep -q '1 of 3 entries synced' "$TMP/t7.err" || fail "synced count missing"
grep -q 'TG_BOT_TOKEN->worker-secret' "$TMP/t7.err" || fail "already-synced list missing"
grep -q 'Rerun is safe' "$TMP/t7.err" || fail "rerun hint missing"
no_leak partial-failure "$TMP/t7.out" "$TMP/t7.err" "$TMP/calls.log"

# 6c. .env syntax rejection: line number reported, value never printed
for bad in 'SUBSONIC_USER= CANARY_LEAD_1' 'SUBSONIC_USER=CANARY_INL_2 # note' 'SUBSONIC_USER="CANARY_Q_3" # note' 'SUBSONIC_USER="CANARY_UNB_4'; do
  grep -v '^SUBSONIC_USER' "$TMP/env.full" > "$TMP/env"; printf '%s\n' "$bad" >> "$TMP/env"
  rm -f "$TMP/calls.log"
  run "$TMP/t8" && fail "bad .env line accepted: ${bad%%CANARY*}"
  grep -q "line $(wc -l < "$TMP/env"): .*(key SUBSONIC_USER)" "$TMP/t8.err" || fail "line number/key not reported"
  [ ! -e "$TMP/calls.log" ] || fail "synced despite bad .env"
  no_leak badenv "$TMP/t8.out" "$TMP/t8.err"; if grep -q CANARY_ "$TMP/t8.out" "$TMP/t8.err"; then fail "bad-line value printed"; fi
done
cp "$TMP/env.full" "$TMP/env"

# 6d. preflight: missing wrangler.local.toml / npx not on PATH abort before any call
mkdir -p "$TMP/nowrangler/scripts" "$TMP/nowrangler/worker"; cp "$ROOT/scripts/sync-secrets.sh" "$ROOT/scripts/secrets.manifest" "$TMP/nowrangler/scripts/"
rm -f "$TMP/calls.log"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" "$TMP/nowrangler/scripts/sync-secrets.sh" > "$TMP/t9.out" 2> "$TMP/t9.err" && fail "missing wrangler.local.toml should fail"
grep -q 'wrangler.local.toml is missing' "$TMP/t9.err" || fail "toml preflight message"
[ ! -e "$TMP/calls.log" ] || fail "call made before preflight failed"
no_leak preflight "$TMP/t9.out" "$TMP/t9.err"
mkdir -p "$TMP/emptybin"; ln -s "$(command -v bash)" "$TMP/emptybin/bash"; ln -s "$(command -v dirname)" "$TMP/emptybin/dirname"
PATH="$TMP/emptybin" ENV_FILE="$TMP/env" "$TMP/emptybin/bash" "$SCRIPT" > "$TMP/t10.out" 2> "$TMP/t10.err" && fail "missing npx should fail"
grep -q 'npx not found' "$TMP/t10.err" || fail "npx preflight message"

# 7. the real manifest holds exactly the three Worker secrets and nothing for GitHub
[ "$(grep -cvE '^[[:space:]]*(#.*)?$' "$ROOT/scripts/secrets.manifest")" -eq 3 ] || fail "manifest must have exactly 3 entries"
for e in 'TG_BOT_TOKEN worker-secret' 'SUBSONIC_USER worker-secret' 'SUBSONIC_PASSWORD worker-secret'; do
  grep -qx "$e" "$ROOT/scripts/secrets.manifest" || fail "manifest lacks: $e"
done
if grep -qE '^[A-Z0-9_]+ gh-' "$ROOT/scripts/secrets.manifest"; then fail "GitHub targets must not be in the manifest"; fi

echo "all sync-secrets tests passed"

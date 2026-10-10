#!/usr/bin/env bash
# Test for sync-secrets.sh: temp .env fixture + gh/npx PATH shims. Never touches the network.
# Proves values reach the tools only via stdin and never appear in stdout/stderr (also under xtrace).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/sync-secrets.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

# Shims: log argv (never stdin) to calls.log, cwd to pwd.log, stdin to stdin.log. FAIL_AT=N makes the
# Nth call exit 1 (after recording). The shims set +x themselves: they stand in for non-bash tools.
for tool in gh npx; do
  cat > "$TMP/bin/$tool" <<SHIM
#!/usr/bin/env bash
set +x
printf '%s\n' "$tool \$*" >> "$TMP/calls.log"
printf '%s\n' "$tool \$PWD" >> "$TMP/pwd.log"
printf '%s: %s\n' "$tool \$*" "\$(cat)" >> "$TMP/stdin.log"
if [ -n "\${FAIL_AT:-}" ] && [ "\$(wc -l < "$TMP/calls.log")" -eq "\$FAIL_AT" ]; then echo "shim: simulated failure" >&2; exit 1; fi
SHIM
  chmod +x "$TMP/bin/$tool"
done

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
grep -q 'INGEST_CLOUDFLARE_API_TOKEN.*gh-secret.*CLOUDFLARE_API_TOKEN.*present' "$TMP/t1.out" || fail "dry-run output"
no_leak dry-run "$TMP/t1.out" "$TMP/t1.err"

# 2. real sync (shims): correct calls, values only on stdin, no leak even under xtrace
(cd "$TMP" && env PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" SHELLOPTS=xtrace "$SCRIPT") > "$TMP/t2.out" 2> "$TMP/t2.err" || fail "sync exit"
no_leak sync "$TMP/t2.out" "$TMP/t2.err" "$TMP/calls.log"
[ "$(wc -l < "$TMP/calls.log")" -eq 10 ] || fail "expected 10 tool calls"
grep -qx 'gh secret set CLOUDFLARE_API_TOKEN: CANARY_INGEST_3333' "$TMP/stdin.log" || fail "renamed secret"
grep -qx 'gh variable set D1_DATABASE_ID: CANARY_D1_6666' "$TMP/stdin.log" || fail "variable"
grep -qx 'npx --no-install wrangler secret put SUBSONIC_USER -c wrangler.local.toml: CANARY_USER_7b21' "$TMP/stdin.log" || fail "worker secret, quote stripping"
grep -qx 'gh secret set TG_BOT_TOKEN: CANARY_TG_9f3a' "$TMP/stdin.log" || fail "export prefix"
grep -q CANARY_BROAD "$TMP/stdin.log" && fail "unmanifested key synced"
grep -qx "gh $ROOT" "$TMP/pwd.log" || fail "gh not run from repo root"
[ "$(grep -c '^npx ' "$TMP/pwd.log")" -eq 3 ] || fail "expected 3 npx calls"
[ "$(grep -cx "npx $ROOT/worker" "$TMP/pwd.log")" -eq 3 ] || fail "npx not run from worker/"
grep -q 'npx --no-install wrangler secret put' "$TMP/calls.log" || fail "npx --no-install not used"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" bash -x "$SCRIPT" --only SUBSONIC_USER > /dev/null 2> "$TMP/t2x.err" || fail "bash -x exit"
no_leak "script resists xtrace via its own set +x" "$TMP/t2x.err"

# 3. --only syncs exactly one entry
rm -f "$TMP/calls.log" "$TMP/stdin.log"
run "$TMP/t3" --only SPOTIFY_CLIENT_ID || fail "--only exit"
[ "$(wc -l < "$TMP/calls.log")" -eq 1 ] || fail "--only call count"
no_leak only "$TMP/t3.out" "$TMP/t3.err"

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

# 6. unknown target / bad manifest fails before any call
printf 'TG_BOT_TOKEN gh-secrte\n' > "$TMP/bad.manifest"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" MANIFEST="$TMP/bad.manifest" "$SCRIPT" > "$TMP/t6.out" 2> "$TMP/t6.err" && fail "unknown target should fail"
grep -q "unknown target" "$TMP/t6.err" || fail "unknown target message"
[ ! -e "$TMP/calls.log" ] || fail "call made with bad manifest"
no_leak badmanifest "$TMP/t6.out" "$TMP/t6.err"

# 6b. partial failure: 3rd of 10 calls fails
rm -f "$TMP/calls.log" "$TMP/stdin.log" "$TMP/pwd.log"
rc=0; FAIL_AT=3 run "$TMP/t7" || rc=$?
[ "$rc" -eq 1 ] || fail "partial failure exit code is $rc, expected 1"
[ "$(wc -l < "$TMP/calls.log")" -eq 3 ] || fail "script continued after a failed call"
grep -q 'failed at SUBSONIC_USER' "$TMP/t7.err" || fail "failing entry not named"
grep -q '2 of 10 entries synced' "$TMP/t7.err" || fail "synced count missing"
grep -q 'TG_BOT_TOKEN->gh-secret' "$TMP/t7.err" || fail "already-synced list missing"
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

# 6d. preflight: missing wrangler.local.toml / gh not on PATH abort before any call
mkdir -p "$TMP/fake/scripts" "$TMP/fake/worker"; cp "$SCRIPT" "$TMP/fake/scripts/"
rm -f "$TMP/calls.log"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" MANIFEST="$ROOT/scripts/secrets.manifest" "$TMP/fake/scripts/sync-secrets.sh" > "$TMP/t9.out" 2> "$TMP/t9.err" && fail "missing wrangler.local.toml should fail"
grep -q 'wrangler.local.toml is missing' "$TMP/t9.err" || fail "toml preflight message"
[ ! -e "$TMP/calls.log" ] || fail "call made before preflight failed"
no_leak preflight "$TMP/t9.out" "$TMP/t9.err"
mkdir -p "$TMP/emptybin"; ln -s "$(command -v bash)" "$TMP/emptybin/bash"; ln -s "$(command -v dirname)" "$TMP/emptybin/dirname"
PATH="$TMP/emptybin" ENV_FILE="$TMP/env" "$TMP/emptybin/bash" "$SCRIPT" > "$TMP/t10.out" 2> "$TMP/t10.err" && fail "missing gh should fail"
grep -q 'gh not found' "$TMP/t10.err" || fail "gh preflight message"

# 7. real manifest has the required entries
for e in 'TG_BOT_TOKEN gh-secret' 'TG_BOT_TOKEN worker-secret' 'INGEST_CLOUDFLARE_API_TOKEN gh-secret:CLOUDFLARE_API_TOKEN' 'D1_DATABASE_ID gh-variable'; do
  grep -qx "$e" "$ROOT/scripts/secrets.manifest" || fail "manifest lacks: $e"
done

echo "all sync-secrets tests passed"

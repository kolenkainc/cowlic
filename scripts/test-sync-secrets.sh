#!/usr/bin/env bash
# Test for sync-secrets.sh: temp .env fixture + gh/npx PATH shims. Never touches the network.
# Proves values reach the tools only via stdin and never appear in stdout/stderr (also under xtrace).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/sync-secrets.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

# Shims: log argv (never stdin) to calls.log, store stdin as "<tool> <args>: <value>" in stdin.log.
for tool in gh npx; do
  cat > "$TMP/bin/$tool" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "$tool \$*" >> "$TMP/calls.log"
printf '%s: %s\n' "$tool \$*" "\$(cat)" >> "$TMP/stdin.log"
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
  for c in "${CANARIES[@]}" CANARY_BROAD_9999; do
    if grep -q "$c" "$@" 2>/dev/null; then fail "$label: value $c leaked"; fi
  done
}

# 1. dry run: all present, nothing executed, no values shown
run "$TMP/t1" --dry-run || fail "dry-run exit"
[ ! -e "$TMP/calls.log" ] || fail "dry-run called tools"
grep -q 'INGEST_CLOUDFLARE_API_TOKEN.*gh-secret.*CLOUDFLARE_API_TOKEN.*present' "$TMP/t1.out" || fail "dry-run output"
no_leak dry-run "$TMP/t1.out" "$TMP/t1.err"

# 2. real sync (shims): correct calls, values only on stdin, no leak even under xtrace
env PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" SHELLOPTS=xtrace "$SCRIPT" > "$TMP/t2.out" 2> "$TMP/t2.err" || fail "sync exit"
no_leak sync "$TMP/t2.out" "$TMP/t2.err" "$TMP/calls.log"
[ "$(wc -l < "$TMP/calls.log")" -eq 10 ] || fail "expected 10 tool calls"
grep -qx 'gh secret set CLOUDFLARE_API_TOKEN: CANARY_INGEST_3333' "$TMP/stdin.log" || fail "renamed secret"
grep -qx 'gh variable set D1_DATABASE_ID: CANARY_D1_6666' "$TMP/stdin.log" || fail "variable"
grep -qx 'npx wrangler secret put SUBSONIC_USER -c wrangler.local.toml: CANARY_USER_7b21' "$TMP/stdin.log" || fail "worker secret, quote stripping"
grep -qx 'gh secret set TG_BOT_TOKEN: CANARY_TG_9f3a' "$TMP/stdin.log" || fail "export prefix"
grep -q CANARY_BROAD "$TMP/stdin.log" && fail "unmanifested key synced"
PATH="$TMP/bin:$PATH" ENV_FILE="$TMP/env" bash -x "$SCRIPT" --only SUBSONIC_USER > /dev/null 2> "$TMP/t2x.err" || fail "bash -x exit"
no_leak xtrace "$TMP/t2x.err"

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

# 7. real manifest has the required entries
for e in 'TG_BOT_TOKEN gh-secret' 'TG_BOT_TOKEN worker-secret' 'INGEST_CLOUDFLARE_API_TOKEN gh-secret:CLOUDFLARE_API_TOKEN' 'D1_DATABASE_ID gh-variable'; do
  grep -qx "$e" "$ROOT/scripts/secrets.manifest" || fail "manifest lacks: $e"
done

echo "all sync-secrets tests passed"

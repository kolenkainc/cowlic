#!/usr/bin/env bash
# Distribute secrets from the local .env to GitHub (secrets/variables) and Worker secrets.
# Usage: scripts/sync-secrets.sh [--dry-run] [--only KEY]
# Values are never printed: they travel only through stdin of gh / wrangler.
# Env overrides (used by tests): ENV_FILE, MANIFEST.
set +x            # never trace: xtrace would expose values
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
MANIFEST="${MANIFEST:-$ROOT/scripts/secrets.manifest}"
NAME_RE='^[A-Za-z_][A-Za-z0-9_]*$'

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

dry_run=0
only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --only)
      [ $# -ge 2 ] || die "--only needs a KEY"
      only="$2"; shift ;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
if [ -n "$only" ] && ! [[ "$only" =~ $NAME_RE ]]; then die "invalid key name for --only"; fi

[ -r "$MANIFEST" ] || die "manifest not readable: $MANIFEST"
[ -r "$ENV_FILE" ] || die "env file not readable: $ENV_FILE"

# Parse .env as data (no source/eval). Supports KEY=VALUE, optional 'export ', optional matching quotes.
declare -A ENVV=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
  key="${BASH_REMATCH[2]}"
  val="${BASH_REMATCH[3]}"
  val="${val%"${val##*[![:space:]]}"}"
  if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then val="${BASH_REMATCH[1]}"; fi
  ENVV["$key"]="$val"
done < "$ENV_FILE"

# Parse and validate the whole manifest before touching anything.
keys=(); targets=(); remotes=()
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
  read -r key spec extra <<< "$line"
  [ -z "${extra:-}" ] || die "manifest line $lineno: too many fields"
  [ -n "${spec:-}" ] || die "manifest line $lineno: expected '<ENV_KEY> <target>[:<REMOTE_NAME>]'"
  [[ "$key" =~ $NAME_RE ]] || die "manifest line $lineno: invalid key name"
  target="${spec%%:*}"
  remote="$key"
  if [[ "$spec" == *:* ]]; then remote="${spec#*:}"; fi
  [[ "$remote" =~ $NAME_RE ]] || die "manifest line $lineno: invalid remote name"
  case "$target" in
    gh-secret|gh-variable|worker-secret) ;;
    *) die "manifest line $lineno: unknown target '$target'" ;;
  esac
  keys+=("$key"); targets+=("$target"); remotes+=("$remote")
done < "$MANIFEST"

selected=()
for i in "${!keys[@]}"; do
  if [ -z "$only" ] || [ "${keys[$i]}" = "$only" ]; then selected+=("$i"); fi
done
[ "${#selected[@]}" -gt 0 ] || die "no manifest entries selected${only:+ (key '$only' is not in the manifest)}"

missing=0
for i in "${selected[@]}"; do
  if [ -n "${ENVV[${keys[$i]}]:-}" ]; then state=present; else state=missing; missing=$((missing + 1)); fi
  if [ "$dry_run" -eq 1 ]; then printf '%-30s %-14s -> %-30s %s\n' "${keys[$i]}" "${targets[$i]}" "${remotes[$i]}" "$state"; fi
  if [ "$dry_run" -eq 0 ] && [ "$state" = missing ]; then printf 'error: %s is missing or empty in %s\n' "${keys[$i]}" "$ENV_FILE" >&2; fi
done
[ "$missing" -eq 0 ] || die "$missing key(s) missing or empty in the env file; nothing was synced"
[ "$dry_run" -eq 0 ] || { echo "dry run OK"; exit 0; }

for i in "${selected[@]}"; do
  key="${keys[$i]}"; remote="${remotes[$i]}"
  printf 'sync %s -> %s %s\n' "$key" "${targets[$i]}" "$remote"
  case "${targets[$i]}" in
    gh-secret)     printf '%s' "${ENVV[$key]}" | gh secret set "$remote" ;;
    gh-variable)   printf '%s' "${ENVV[$key]}" | gh variable set "$remote" ;;
    worker-secret) printf '%s' "${ENVV[$key]}" | (cd "$ROOT/worker" && npx wrangler secret put "$remote" -c wrangler.local.toml) ;;
  esac
done
echo "done"

#!/usr/bin/env bash
# Distribute secrets from the local .env to GitHub environment secrets and Worker secrets.
# Ingest values go to the GitHub environment "ingest" (gh secret set --env), see scripts/secrets.manifest.
# Usage: scripts/sync-secrets.sh [--dry-run] [--only KEY]
# Values are never printed: they travel only through stdin of gh / wrangler.
# Env overrides (used by tests): ENV_FILE, MANIFEST.
#
# Supported .env subset (anything else is rejected with the line number, never the value):
#   KEY=value, optional leading "export ", optional matching single or double quotes around the
#   whole value. The value is taken verbatim: no escape processing (a backslash is a backslash),
#   no variable expansion, no multiline values. Trailing whitespace is trimmed. A value that starts
#   with a space, or an unquoted value containing " #" (inline comment), is rejected. Comment lines
#   (starting with #) and blank lines are ignored. If a key is repeated, the last assignment wins.
set +x            # never trace: xtrace would expose values
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
MANIFEST="${MANIFEST:-$ROOT/scripts/secrets.manifest}"
cd "$ROOT"   # gh resolves the target repo from the cwd; never depend on where the user ran us
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

# Parse .env as data (no source/eval). See the supported subset in the header.
declare -A ENVV=()
declare -A BADLINE=()
envline=0
while IFS= read -r line || [ -n "$line" ]; do
  envline=$((envline + 1))
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
  key="${BASH_REMATCH[2]}"
  val="${BASH_REMATCH[3]}"
  val="${val%"${val##*[![:space:]]}"}"
  unset 'BADLINE[$key]'
  if [[ "$val" =~ ^[[:space:]] ]]; then
    BADLINE["$key"]="$envline:value starts with whitespace"; ENVV["$key"]=""; continue
  fi
  if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
    val="${BASH_REMATCH[1]}"
  elif [[ "$val" == \"* || "$val" == \'* ]]; then
    BADLINE["$key"]="$envline:unbalanced quote or text after the closing quote"; ENVV["$key"]=""; continue
  elif [[ "$val" =~ [[:space:]]# ]]; then
    BADLINE["$key"]="$envline:unquoted inline comment (' #')"; ENVV["$key"]=""; continue
  fi
  ENVV["$key"]="$val"
done < "$ENV_FILE"

# Parse and validate the whole manifest before touching anything.
declare -A DESTS=()   # needs bash >= 4 (also used for ENVV below)
keys=(); targets=(); remotes=(); ghenvs=()
lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
  read -r key spec extra <<< "$line"
  [ -z "${extra:-}" ] || die "manifest line $lineno: too many fields"
  [ -n "${spec:-}" ] || die "manifest line $lineno: expected '<ENV_KEY> <target>[@<GH_ENVIRONMENT>][:<REMOTE_NAME>]'"
  [[ "$key" =~ $NAME_RE ]] || die "manifest line $lineno: invalid key name"
  target="${spec%%:*}"
  ghenv=""
  if [[ "$target" == *@* ]]; then ghenv="${target#*@}"; target="${target%%@*}"; fi
  remote="$key"
  if [[ "$spec" == *:* ]]; then remote="${spec#*:}"; fi
  [[ "$remote" =~ $NAME_RE ]] || die "manifest line $lineno: invalid remote name"
  case "$target" in
    gh-env-secret)
      [[ "$ghenv" =~ $NAME_RE ]] || die "manifest line $lineno: gh-env-secret needs an environment name, e.g. gh-env-secret@ingest" ;;
    worker-secret)
      [ -z "$ghenv" ] || die "manifest line $lineno: '@environment' is only valid for gh-env-secret" ;;
    *) die "manifest line $lineno: unknown target '$target'" ;;
  esac
  dest="$target@$ghenv:$remote"
  [ -z "${DESTS[$dest]:-}" ] || die "manifest line $lineno: duplicate destination $dest (first defined on line ${DESTS[$dest]})"
  DESTS["$dest"]="$lineno"
  keys+=("$key"); targets+=("$target"); remotes+=("$remote"); ghenvs+=("$ghenv")
done < "$MANIFEST"

selected=()
for i in "${!keys[@]}"; do
  if [ -z "$only" ] || [ "${keys[$i]}" = "$only" ]; then selected+=("$i"); fi
done
[ "${#selected[@]}" -gt 0 ] || die "no manifest entries selected${only:+ (key '$only' is not in the manifest)}"

badcount=0
for i in "${selected[@]}"; do
  b="${BADLINE[${keys[$i]}]:-}"
  if [ -n "$b" ]; then
    printf 'error: %s line %s: %s (key %s)\n' "$ENV_FILE" "${b%%:*}" "${b#*:}" "${keys[$i]}" >&2
    badcount=$((badcount + 1))
  fi
done
[ "$badcount" -eq 0 ] || die "unsupported .env syntax; nothing was synced"

missing=0
for i in "${selected[@]}"; do
  if [ -n "${ENVV[${keys[$i]}]:-}" ]; then state=present; else state=missing; missing=$((missing + 1)); fi
  if [ "$dry_run" -eq 1 ]; then printf '%-30s %-26s -> %-30s %s\n' "${keys[$i]}" "${targets[$i]}${ghenvs[$i]:+@${ghenvs[$i]}}" "${remotes[$i]}" "$state"; fi
  if [ "$dry_run" -eq 0 ] && [ "$state" = missing ]; then printf 'error: %s is missing or empty in %s\n' "${keys[$i]}" "$ENV_FILE" >&2; fi
done
[ "$missing" -eq 0 ] || die "$missing key(s) missing or empty in the env file; nothing was synced"
[ "$dry_run" -eq 0 ] || { echo "dry run OK"; exit 0; }

# Preflight before any write: tools and files the selected entries need.
need_gh=0; need_worker=0
for i in "${selected[@]}"; do
  case "${targets[$i]}" in gh-env-secret) need_gh=1 ;; worker-secret) need_worker=1 ;; esac
done
if [ "$need_gh" -eq 1 ]; then command -v gh > /dev/null || die "gh not found on PATH; nothing was synced"; fi
if [ "$need_worker" -eq 1 ]; then
  command -v npx > /dev/null || die "npx not found on PATH; nothing was synced"
  [ -r "$ROOT/worker/wrangler.local.toml" ] || die "worker/wrangler.local.toml is missing (gitignored, holds the real D1 id); nothing was synced"
fi
# Every GitHub environment found in the selected entries is probed here, for all entries, before the
# first write (independent of manifest order). The probe is the endpoint `gh secret set --env` itself
# needs (the environment's public key), so it checks the permission the writes need. {owner}/{repo}
# are resolved by gh from the clone (we cd to the repo root). gh's first stderr line is shown (it
# carries the HTTP status, never a secret value).
declare -A seen_env=()
for i in "${selected[@]}"; do
  e="${ghenvs[$i]}"
  [ -n "$e" ] && [ -z "${seen_env[$e]:-}" ] || continue
  seen_env["$e"]=1
  perr="$(gh api "repos/{owner}/{repo}/environments/$e/secrets/public-key" 2>&1 > /dev/null < /dev/null)" && continue
  first="$(printf '%s\n' "$perr" | head -n 1 | tr -d '\000-\037' | cut -c1-200)"
  case "$perr" in
    *"HTTP 401"*) why="not authenticated or bad token (401): run gh auth login / check GH_TOKEN" ;;
    *"HTTP 404"*) why="environment not found, or the token cannot see the repository (404): create the environment (restricted to master) in the repo settings" ;;
    *"HTTP 403"*) why="forbidden (403): the token lacks permission to write environment secrets" ;;
    *) why="request failed (network or other error)" ;;
  esac
  die "GitHub environment '$e' preflight failed: $why. gh said: ${first:-<no output>}. Nothing was synced"
done

synced=()
total="${#selected[@]}"
for i in "${selected[@]}"; do
  key="${keys[$i]}"; remote="${remotes[$i]}"; target="${targets[$i]}"; ghenv="${ghenvs[$i]}"
  printf 'sync %s -> %s %s\n' "$key" "$target${ghenv:+@$ghenv}" "$remote"
  rc=0
  case "$target" in
    gh-env-secret) printf '%s' "${ENVV[$key]}" | gh secret set "$remote" --env "$ghenv" || rc=$? ;;
    worker-secret) printf '%s' "${ENVV[$key]}" | (cd "$ROOT/worker" && npx --no-install wrangler secret put "$remote" -c wrangler.local.toml) || rc=$? ;;
  esac
  if [ "$rc" -ne 0 ]; then
    printf 'error: failed at %s -> %s %s (exit %s); %s of %s entries synced before it' "$key" "$target" "$remote" "$rc" "${#synced[@]}" "$total" >&2
    printf '. Already synced: %s. Rerun is safe (idempotent upserts); use --only KEY to retry one entry.\n' "${synced[*]:-none}" >&2
    exit "$rc"
  fi
  synced+=("$key->$target")
done
echo "done: $total entries synced"

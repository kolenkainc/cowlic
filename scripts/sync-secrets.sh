#!/usr/bin/env bash
# Distribute secrets from the local .env to Worker secrets (see scripts/secrets.manifest).
# Ingest runs locally and reads the .env itself, so nothing is written to GitHub.
# Usage: scripts/sync-secrets.sh [--dry-run] [--only KEY]
# Values are never printed: they travel only through stdin of wrangler.
# Env overrides (used by tests): ENV_FILE, MANIFEST.
#
# Supported .env subset (anything else is rejected with the line number, never the value). The same
# rules are implemented in ingest/tg_check.py parse_env and scripts/check-local-prereqs.sh; the shared
# fixtures in scripts/testdata/env-syntax keep the three in step:
#   KEY=value with no space around "=", optional matching single or double quotes around the whole
#   value. No "export". The value is taken verbatim: no escape processing (a backslash is a backslash),
#   no variable expansion, no multiline values. Trailing spaces/tabs are trimmed. A value that starts
#   with a space, an unbalanced quote, or an unquoted value containing " #" (inline comment) is
#   rejected, and so is a repeated key. Comment lines (first non-blank char #) and blank lines are
#   ignored; any other line is rejected.
set +x            # never trace: xtrace would expose values
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
MANIFEST="${MANIFEST:-$ROOT/scripts/secrets.manifest}"
cd "$ROOT"   # never depend on where the user ran us
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
declare -A ENVSEEN=()
envline=0
envbad=0
ws=$' \t'
re_skip='^[ '$'\t'']*(#.*)?$'
re_line='^[ '$'\t'']*([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
re_dq='^"(.*)"$'
re_sq="^'(.*)'\$"
re_comment='[ '$'\t'']#'
envbadmsg() { printf 'error: %s line %s: %s\n' "$ENV_FILE" "$envline" "$1" >&2; envbad=$((envbad + 1)); }
while IFS= read -r line || [ -n "$line" ]; do
  envline=$((envline + 1))
  line="${line%$'\r'}"
  [[ "$line" =~ $re_skip ]] && continue
  if ! [[ "$line" =~ $re_line ]]; then
    if [[ "$line" =~ ^[$ws]*export[$ws] ]]; then envbadmsg "'export' is not supported, write KEY=value"
    else envbadmsg "not a KEY=value line (no spaces around '=')"; fi
    continue
  fi
  key="${BASH_REMATCH[1]}"
  val="${BASH_REMATCH[2]}"
  while [[ "$val" == *[$ws] ]]; do val="${val%?}"; done
  if [ -n "${ENVSEEN[$key]:-}" ]; then envbadmsg "key $key is defined twice"; fi
  ENVSEEN["$key"]=1
  if [[ "$val" == [$ws]* ]]; then
    envbadmsg "value starts with whitespace (key $key)"; continue
  fi
  if [[ "$val" =~ $re_dq ]] || [[ "$val" =~ $re_sq ]]; then
    val="${BASH_REMATCH[1]}"
  elif [[ "$val" == \"* || "$val" == \'* ]]; then
    envbadmsg "unbalanced quote or text after the closing quote (key $key)"; continue
  elif [[ "$val" =~ $re_comment ]]; then
    envbadmsg "unquoted inline comment (' #') (key $key)"; continue
  fi
  ENVV["$key"]="$val"
done < "$ENV_FILE"
[ "$envbad" -eq 0 ] || die "unsupported .env syntax; nothing was synced"

# Parse and validate the whole manifest before touching anything.
declare -A DESTS=()   # needs bash >= 4 (also used for ENVV below)
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
    worker-secret) ;;
    *) die "manifest line $lineno: unknown target '$target'" ;;
  esac
  dest="$target:$remote"
  [ -z "${DESTS[$dest]:-}" ] || die "manifest line $lineno: duplicate destination $dest (first defined on line ${DESTS[$dest]})"
  DESTS["$dest"]="$lineno"
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
  if [ "$dry_run" -eq 1 ]; then printf '%-30s %-26s -> %-30s %s\n' "${keys[$i]}" "${targets[$i]}" "${remotes[$i]}" "$state"; fi
  if [ "$dry_run" -eq 0 ] && [ "$state" = missing ]; then printf 'error: %s is missing or empty in %s\n' "${keys[$i]}" "$ENV_FILE" >&2; fi
done
[ "$missing" -eq 0 ] || die "$missing key(s) missing or empty in the env file; nothing was synced"
[ "$dry_run" -eq 0 ] || { echo "dry run OK"; exit 0; }

# Preflight before any write: tools and files the selected entries need.
command -v npx > /dev/null || die "npx not found on PATH; nothing was synced"
[ -r "$ROOT/worker/wrangler.local.toml" ] || die "worker/wrangler.local.toml is missing (gitignored, holds the real D1 id); nothing was synced"

synced=()
total="${#selected[@]}"
for i in "${selected[@]}"; do
  key="${keys[$i]}"; remote="${remotes[$i]}"; target="${targets[$i]}"
  printf 'sync %s -> %s %s\n' "$key" "$target" "$remote"
  rc=0
  case "$target" in
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

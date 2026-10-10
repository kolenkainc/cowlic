#!/usr/bin/env bash
# Read-only check that this machine can run the local ingest. No network, installs nothing,
# never prints values of .env (it greps the file to see which required keys are defined with a non-empty value).
# One line per check: "ok" or "MISSING" with a fix hint. Exit 1 if anything is missing.
#
# Install hints (Debian/Ubuntu | macOS with Homebrew):
#   ffmpeg   sudo apt install ffmpeg                    | brew install ffmpeg
#   deno     >= 2.3.0, yt-dlp's JavaScript runtime. The old workflow pinned 2.9.5:
#            https://github.com/denoland/deno/releases (verify the checksum) | brew install deno
#   python   sudo apt install python3 python3-venv      | brew install python@3.12   (3.10+)
#   venv     python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt
#   flock    sudo apt install util-linux                | brew install flock (not part of macOS)
#   bash     >= 4: already true on Debian/Ubuntu         | brew install bash (macOS ships 3.2)
#   .env     copy the template from the repo docs and fill it in (see the runbook); this script
#            only checks the key names, never the values.
# Option --deno-optional: deno missing, too old or unparsable prints a WARN line and keeps the exit code (the
# local wrapper uses it; ingest.py runs without deno and only logs a warning). Everything else stays fatal.
# Test: scripts/test-check-local-prereqs.sh
set +x   # never trace: xtrace would expose values of .env that grep reads
set -u

ROOT="${CHECK_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$ROOT/.env"
VENV_PY="$ROOT/ingest/.venv/bin/python"
MIN_DENO="2.3.0"
# Copies of REQUIRED_ENV and MIN_DENO in ingest/ingest.py; scripts/test-check-local-prereqs.sh fails on drift.
REQUIRED_ENV=(TG_BOT_TOKEN TG_CHANNEL_ID SPOTIFY_CLIENT_ID SPOTIFY_CLIENT_SECRET
              INGEST_CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID D1_DATABASE_ID)
TIMEOUT_S=30  # as in ingest.py for deno; skipped when `timeout` is not installed (stock macOS)

DENO_OPTIONAL=0
case "${1:-}" in
  "") ;;
  --deno-optional) DENO_OPTIONAL=1 ;;
  *) echo "usage: $0 [--deno-optional]" >&2; exit 2 ;;
esac

fail=0
with_timeout() { if command -v timeout >/dev/null 2>&1; then timeout "$TIMEOUT_S" "$@"; else "$@"; fi; }
ok() { printf 'ok       %s\n' "$1"; }
missing() { printf 'MISSING  %s: %s\n' "$1" "$2"; fail=1; }

# version_ge A B: true when dotted version A >= B (numeric per component, so 2.10.0 > 2.3.0).
version_ge() {
  local IFS=. i
  local -a a=($1) b=($2)
  for i in 0 1 2; do
    local x=$((10#${a[i]:-0})) y=$((10#${b[i]:-0}))
    ((x > y)) && return 0
    ((x < y)) && return 1
  done
  return 0
}

if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg"
else missing "ffmpeg" "not on PATH. Debian/Ubuntu: sudo apt install ffmpeg; macOS: brew install ffmpeg"; fi

# With --deno-optional a deno problem is a "WARN" line and does not change the exit code
# (ingest.py itself only warns about a missing JS runtime).
deno_problem() {
  if ((DENO_OPTIONAL)); then printf 'WARN     %s: %s\n' "$1" "$2"; else missing "$1" "$2"; fi
}
if ! command -v deno >/dev/null 2>&1; then
  deno_problem "deno" "not on PATH, need >= $MIN_DENO. macOS: brew install deno; Linux: release from https://github.com/denoland/deno/releases"
else
  dv="$(with_timeout deno --version 2>/dev/null | head -n 1)"
  if [[ "$dv" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    if version_ge "${BASH_REMATCH[1]}" "$MIN_DENO"; then ok "deno ${BASH_REMATCH[1]} (>= $MIN_DENO)"
    else deno_problem "deno" "version ${BASH_REMATCH[1]} is older than $MIN_DENO. Upgrade: deno upgrade, or brew upgrade deno"; fi
  else
    deno_problem "deno" "cannot parse 'deno --version' output; need >= $MIN_DENO"
  fi
fi

if [ -x "$VENV_PY" ]; then
  ok "ingest/.venv"
  if with_timeout "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then ok "python >= 3.10 (venv)"
  else missing "python" "venv python is older than 3.10. Recreate: rm -rf ingest/.venv; python3 -m venv ingest/.venv (with a newer python3)"; fi
  for mod in spotdl ytmusicapi; do
    if with_timeout "$VENV_PY" -c "import $mod" >/dev/null 2>&1; then ok "python module $mod"
    else missing "python module $mod" "not importable in ingest/.venv. Run: ingest/.venv/bin/pip install -r ingest/requirements.txt"; fi
  done
else
  missing "ingest/.venv" "not found. Run: python3 -m venv ingest/.venv && ingest/.venv/bin/pip install -r ingest/requirements.txt (Debian/Ubuntu: sudo apt install python3-venv first)"
  if command -v python3 >/dev/null 2>&1; then
    if with_timeout python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then ok "python3 >= 3.10"
    else missing "python3" "older than 3.10. Debian/Ubuntu: sudo apt install python3; macOS: brew install python@3.12"; fi
  else
    missing "python3" "not on PATH. Debian/Ubuntu: sudo apt install python3 python3-venv; macOS: brew install python@3.12"
  fi
fi

if command -v flock >/dev/null 2>&1; then ok "flock"
else missing "flock" "not on PATH. Debian/Ubuntu: sudo apt install util-linux; macOS: brew install flock"; fi

if ((BASH_VERSINFO[0] >= 4)); then ok "bash ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]} (>= 4)"
else missing "bash" "version ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]} is older than 4. macOS: brew install bash and use it for the wrapper"; fi

if [ -f "$ENV_FILE" ] && [ -r "$ENV_FILE" ]; then
  # Same rules as tg_check.load_env + ingest.py: a line counts when it has "=" and its stripped text does not
  # start with "#"; the key is the stripped text before the first "=" (so "export KEY=v" defines "export KEY");
  # the first definition of a key wins; an empty value counts as missing. Values never reach output.
  absent=() ; exported=()
  for k in "${REQUIRED_ENV[@]}"; do
    if ! grep -E "^[[:space:]]*$k[[:space:]]*=" "$ENV_FILE" 2>/dev/null | head -n 1 \
         | grep -Eq "=[[:space:]]*[^[:space:]]" >/dev/null 2>&1; then
      absent+=("$k")
      grep -Eq "^[[:space:]]*export[[:space:]]+$k[[:space:]]*=" "$ENV_FILE" >/dev/null 2>&1 && exported+=("$k")
    fi
  done
  if ((${#absent[@]} == 0)); then ok ".env has all required keys"
  else
    hint=""
    ((${#exported[@]})) && hint=" ('export' is not supported, write KEY=value: ${exported[*]})"
    missing ".env" "missing or empty: ${absent[*]}$hint. Fill them in, see the runbook"
  fi
else
  missing ".env" "not found or unreadable at the repo root. Create it with the required keys: ${REQUIRED_ENV[*]}"
fi

if ((fail)); then echo "Some prerequisites are missing; nothing was installed."; exit 1; fi
echo "All prerequisites present."

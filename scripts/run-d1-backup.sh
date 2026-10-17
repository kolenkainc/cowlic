#!/usr/bin/env bash
# .env is loaded only by the Python parser, never sourced as shell code.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$repo_dir/ingest/.venv/bin/python" ]]; then
  echo 'backup-d1: Python venv unavailable'
  exit 78
fi
exec "$repo_dir/ingest/.venv/bin/python" "$repo_dir/scripts/backup-d1.py" "$@"

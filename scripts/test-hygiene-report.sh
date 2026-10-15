#!/usr/bin/env bash
# Offline test for hygiene-report.sh: a throwaway git repo (local bare "origin") plus a STUB gh on PATH that answers from
# fixtures. Never touches the network or the real repository. Run by hand: bash scripts/test-hygiene-report.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
OUT="$(mktemp -d)"  # the report's own output files, outside the snapshot of the repo area
trap 'rm -rf "$TMP" "$OUT"' EXIT

failures=0
assert() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi; }
has() { grep -q -- "$1" "$2" 2> /dev/null && echo 0 || echo 1; }
hasnt() { grep -q -- "$1" "$2" 2> /dev/null && echo 1 || echo 0; }

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid

# --- a repo with a copy of the script, a bare origin, and a few worktrees ---
git init -q --bare -b master "$TMP/origin.git"
git clone -q "$TMP/origin.git" "$TMP/repo" 2> /dev/null
R="$TMP/repo"
mkdir -p "$R/scripts"
cp "$ROOT/scripts/hygiene-report.sh" "$R/scripts/"
git -C "$R" checkout -q -b master 2> /dev/null || true
echo a > "$R/a"; git -C "$R" add a scripts; git -C "$R" commit -q -m base
git -C "$R" push -q origin master 2> /dev/null
git -C "$R" fetch -q origin 2> /dev/null

# merged-clean (branch at master tip), dirty, unmerged, stale (old commit), squash-merged (head of a merged PR)
git -C "$R" worktree add -q -b feat/in-master "$TMP/wt-merged" master
git -C "$R" worktree add -q -b feat/dirty "$TMP/wt-dirty" master
echo x > "$TMP/wt-dirty/untracked"
git -C "$R" worktree add -q -b feat/unmerged "$TMP/wt-unmerged" master
echo u > "$TMP/wt-unmerged/u"; git -C "$TMP/wt-unmerged" add u; git -C "$TMP/wt-unmerged" commit -q -m unmerged
git -C "$R" worktree add -q -b feat/stale "$TMP/wt-stale" master
echo s > "$TMP/wt-stale/s"; git -C "$TMP/wt-stale" add s
GIT_COMMITTER_DATE="2020-01-01T00:00:00Z" git -C "$TMP/wt-stale" commit -q -m stale
git -C "$R" worktree add -q -b feat/squashed "$TMP/wt-squashed" master
echo q > "$TMP/wt-squashed/q"; git -C "$TMP/wt-squashed" add q; git -C "$TMP/wt-squashed" commit -q -m squashed
SQ_SHA="$(git -C "$TMP/wt-squashed" rev-parse HEAD)"
# an injection-looking branch name and a local branch whose PR was merged with a different tip
EVIL='evil$(touch${IFS}PWNED)'
git -C "$R" branch "$EVIL" master
git -C "$R" worktree add -q -b feat/diverged "$TMP/wt-div" master
echo d > "$TMP/wt-div/d"; git -C "$TMP/wt-div" add d; git -C "$TMP/wt-div" commit -q -m div
git -C "$R" worktree add -q --detach "$TMP/wt-gone" master
rm -rf "$TMP/wt-gone"

# --- stub gh ---
BIN="$TMP/bin"; mkdir -p "$BIN"
cat > "$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "auth status") exit "${STUB_AUTH_RC:-0}" ;;
  "pr list") [ "${STUB_PR_FAIL:-0}" = 1 ] && exit 1; cat "$STUB_PRS"; exit 0 ;;
  "api --paginate") cat "$STUB_BRANCHES"; exit 0 ;;
  "issue list") cat "$STUB_ISSUES"; exit 0 ;;
esac
exit 1
STUB
chmod +x "$BIN/gh"

T="$(printf '\t')"
ESC="$(printf '\033')"
{
  printf 'MERGED%sfeat/squashed%s%s%s1\n' "$T" "$T" "$SQ_SHA" "$T"
  printf 'MERGED%sfeat/diverged%s%s%s2\n' "$T" "$T" "0000000000000000000000000000000000000002" "$T"
  printf 'OPEN%sfeat/open-pr%s%s%s3\n' "$T" "$T" "1111111111111111111111111111111111111111" "$T"
} > "$TMP/prs"
{
  echo "master"; echo "feat/open-pr"; echo "feat/orphan-remote"; echo "feat/squashed"
  printf 'bad%s[31mname\n' "$ESC"
} > "$TMP/branches"
{
  printf '10%s2020-01-01T00:00:00Z%s-%sold ticket with no PR %s[31mred\n' "$T" "$T" "$T" "$ESC"
  printf '11%s2020-01-01T00:00:00Z%s3%sold ticket with open PR\n' "$T" "$T" "$T"
  printf '12%s2020-01-01T00:00:00Z%s1%sold ticket whose PR is merged\n' "$T" "$T" "$T"
  printf '13%s2999-01-01T00:00:00Z%s-%sfresh ticket\n' "$T" "$T" "$T"
} > "$TMP/issues"
export STUB_PRS="$TMP/prs" STUB_BRANCHES="$TMP/branches" STUB_ISSUES="$TMP/issues"

snapshot() { { git -C "$R" worktree list --porcelain; git -C "$R" for-each-ref; git -C "$R" status --porcelain; find "$TMP" -path '*/.git' -prune -o -type f -print | sort; } | sha256sum; }
run() { rc=0; (cd "$R" && PATH="$BIN:$PATH" bash scripts/hygiene-report.sh "$@" > "$OUT/out" 2> "$OUT/err") || rc=$?; }

before="$(snapshot)"
run
after="$(snapshot)"
assert "exit code is 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "nothing changed (worktrees, refs, status, files)" "$([ "$before" = "$after" ] && echo 0 || echo 1)"
assert "injection-looking branch name did not execute" "$([ ! -e "$R/PWNED" ] && [ ! -e "$TMP/PWNED" ] && echo 0 || echo 1)"
assert "no escape character in the output" "$(hasnt "$ESC" "$OUT/out")"
assert "five sections" "$([ "$(grep -c '^## [1-5]\.' "$OUT/out")" = 5 ] && echo 0 || echo 1)"
assert "worktree in origin/master is merged-clean" "$(has 'merged-clean .*wt-merged' "$OUT/out")"
assert "dirty worktree is dirty" "$(has 'dirty .*wt-dirty' "$OUT/out")"
assert "unmerged worktree is unmerged" "$(has 'unmerged .*wt-unmerged' "$OUT/out")"
assert "old unmerged worktree is stale" "$(has 'stale .*wt-stale' "$OUT/out")"
assert "squash-merged worktree (head of a merged PR) is merged-clean" "$(has 'merged-clean .*wt-squashed' "$OUT/out")"
assert "missing directory is prunable" "$(has 'prunable .*wt-gone' "$OUT/out")"
assert "main checkout is marked, not classified for deletion" "$(has 'main .*\[main checkout\]' "$OUT/out")"
assert "local branch with merged PR listed" "$(has 'feat/squashed: PR merged' "$OUT/out")"
assert "branch whose PR merged with another tip is flagged" "$(has 'feat/diverged: PR merged but the local tip differs' "$OUT/out")"
assert "remote branch with open PR not listed" "$(hasnt 'feat/open-pr:' "$OUT/out")"
assert "remote branch without PR listed" "$(has 'feat/orphan-remote: no PR' "$OUT/out")"
assert "remote branch with merged PR listed" "$(has 'feat/squashed: PR merged, branch not deleted' "$OUT/out")"
assert "ticket without PR listed" "$(has '#10: last updated' "$OUT/out")"
assert "ticket with an open PR not listed" "$(hasnt '#11:' "$OUT/out")"
assert "ticket whose PR is merged (not open) is listed" "$(has '#12:' "$OUT/out")"
assert "fresh ticket (under the threshold) not listed" "$(hasnt '#13:' "$OUT/out")"

# unauthenticated gh: skip lines, still 0
STUB_AUTH_RC=1 run
assert "unauthenticated gh: exit 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "unauthenticated gh: clear note" "$(has 'gh is not authenticated' "$OUT/out")"
assert "unauthenticated gh: section 5 skipped" "$(has 'skipped: gh is not authenticated' "$OUT/out")"
assert "unauthenticated gh: git-only parts still print" "$(has 'merged-clean .*wt-merged' "$OUT/out")"

# gh missing from PATH: a PATH with symlinks to the needed tools only
NOGH="$TMP/nogh"; mkdir -p "$NOGH"
for t in bash git date tr cut head dirname mkdir sed grep cat sort find sha256sum timeout pgrep; do
  p="$(command -v "$t" || true)"; [ -z "$p" ] || ln -sf "$p" "$NOGH/$t"
done
rc=0; (cd "$R" && PATH="$NOGH" bash scripts/hygiene-report.sh > "$OUT/out" 2> "$OUT/err") || rc=$?
assert "gh missing: exit 0" "$([ "$rc" = 0 ] && echo 0 || echo 1)"
assert "gh missing: clear note" "$(has 'gh is not installed' "$OUT/out")"

# gh pr list failing: still exit 0, PR-based parts skipped
STUB_PR_FAIL=1 run
assert "pr list failure: exit 0 and a note" "$([ "$rc" = 0 ] && [ "$(has 'gh pr list failed' "$OUT/out")" = 0 ] && echo 0 || echo 1)"

# --days: a huge threshold leaves no stale ticket; a bad value falls back to 7
run --days 9999
assert "--days 9999: no ticket listed" "$(hasnt '#10:' "$OUT/out")"
run --days abc
assert "--days abc: falls back, exit 0" "$([ "$rc" = 0 ] && [ "$(has 'age threshold 7 days' "$OUT/out")" = 0 ] && echo 0 || echo 1)"

if [ "$failures" -ne 0 ]; then echo "$failures FAILED"; exit 1; fi
echo "all passed"

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
hasline() { grep -qxF -- "$1" "$2" 2> /dev/null && echo 0 || echo 1; }
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
# a non-ASCII branch name (git forbids control characters, space and glob characters in refs, but not these)
UNI="caf$(printf '\303\251')-$(printf '\342\200\256')x"
git -C "$R" branch "$UNI" master
# a worktree whose .git file is broken: git status / log fail in it, it must never be reported merged-clean
git -C "$R" worktree add -q -b feat/broken "$TMP/wt-broken" master
echo "gitdir: $TMP/does-not-exist" > "$TMP/wt-broken/.git"

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
HOSTILE_REMOTE='rem *$(touch P3) x'
{ # remote branches: name, tip sha
  printf 'master%s%s\n' "$T" "9999999999999999999999999999999999999999"
  printf 'feat/open-pr%s%s\n' "$T" "1111111111111111111111111111111111111111"
  printf 'feat/orphan-remote%s%s\n' "$T" "2222222222222222222222222222222222222222"
  printf 'feat/squashed%s%s\n' "$T" "$SQ_SHA"                                        # tip == merged PR head: deletable
  printf 'feat/diverged%s%s\n' "$T" "3333333333333333333333333333333333333333"      # name reused after merge: tip differs
  printf '%s%s%s\n' "$HOSTILE_REMOTE" "$T" "4444444444444444444444444444444444444444"
  printf 'bad%s[31mname%s%s\n' "$ESC" "$T" "5555555555555555555555555555555555555555"
} > "$TMP/branches"
{ # flag (P = In progress), number, updatedAt, closing PRs, title
  printf 'P%s10%s2020-01-01T00:00:00Z%s-%sold ticket with no PR %s[31mred\n' "$T" "$T" "$T" "$T" "$ESC"
  printf 'P%s11%s2020-01-01T00:00:00Z%s3%sold ticket with open PR\n' "$T" "$T" "$T" "$T"
  printf 'P%s12%s2020-01-01T00:00:00Z%s1%sold ticket whose PR is merged\n' "$T" "$T" "$T" "$T"
  printf 'P%s13%s2999-01-01T00:00:00Z%s-%sfresh ticket\n' "$T" "$T" "$T" "$T"
  printf -- '-%s14%s2020-01-01T00:00:00Z%s-%sold ticket not In progress\n' "$T" "$T" "$T" "$T"
} > "$TMP/issues"
cat > "$BIN/pgrep" <<'STUB'
#!/bin/sh
cat "$STUB_PGREP"
STUB
chmod +x "$BIN/pgrep"
: > "$TMP/pgrep"
export STUB_PRS="$TMP/prs" STUB_BRANCHES="$TMP/branches" STUB_ISSUES="$TMP/issues" STUB_PGREP="$TMP/pgrep"

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
assert "remote branch whose tip is the merged PR head: deletable note" "$(hasline '   feat/squashed: PR merged, tip is the PR head (branch not deleted)' "$OUT/out")"
assert "remote branch reused after merge (tip differs): do not delete" "$(hasline '   feat/diverged: PR merged but the remote tip differs: DO NOT DELETE (extra commits)' "$OUT/out")"
assert "hostile remote name (spaces, *, \$(...)) printed as one exact line" "$(hasline '   rem *$(touch P3) x: no PR' "$OUT/out")"
assert "hostile remote name did not execute or glob" "$([ ! -e "$R/P3" ] && [ ! -e "$TMP/P3" ] && echo 0 || echo 1)"
assert "remote name with an escape character is sanitised to one exact line" "$(hasline '   bad[31mname: no PR' "$OUT/out")"
assert "local hostile name printed as one exact line" "$(hasline '   evil$(touch${IFS}PWNED): tip is in origin/master' "$OUT/out")"
assert "local non-ASCII name: non-ASCII bytes stripped, exact line" "$(hasline '   caf-x: tip is in origin/master' "$OUT/out")"
assert "broken worktree is unknown, never merged-clean" "$([ "$(has 'unknown .*wt-broken' "$OUT/out")" = 0 ] && [ "$(hasnt 'merged-clean .*wt-broken' "$OUT/out")" = 0 ] && echo 0 || echo 1)"
assert "broken worktree explained" "$(has 'git status/log failed' "$OUT/out")"
assert "ticket not In progress not listed" "$(hasnt '#14:' "$OUT/out")"
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

# section 4: stub pgrep; only pid, a kind label and the executable basename may be printed, never an argument
{
  echo "4242 /usr/bin/bash /tmp/x/scripts/run-bootstrap-local.sh run --token SECRETARG123"
  echo "4243 /tmp/x/ingest/.venv/bin/python SECRET2 -u"
  echo "4244 node /opt/wrangler dev --secret=ABC987"
  echo "4245 /usr/bin/env SECRETFIRSTARG workerd serve"
} > "$TMP/pgrep"
run
assert "section 4: wrapper line is exactly pid, kind and executable basename" "$(hasline '   pid 4242: bootstrap test wrapper (bash)' "$OUT/out")"
assert "section 4: stub python and wrangler lines" "$([ "$(hasline '   pid 4243: stub ingest python (python)' "$OUT/out")" = 0 ] && [ "$(hasline '   pid 4244: wrangler dev (node)' "$OUT/out")" = 0 ] && echo 0 || echo 1)"
assert "section 4: no argument or secret-looking text is printed" "$([ "$(hasnt 'SECRETARG123\|SECRET2\|ABC987\|SECRETFIRSTARG\|/opt/wrangler\|/tmp/x' "$OUT/out")" = 0 ] && echo 0 || echo 1)"
assert "section 4: total counts the processes" "$(has 'total: 4 matching processes' "$OUT/out")"
: > "$TMP/pgrep"

# fail closed also when every worktree check works but the answer is empty is covered by the broken worktree above;
# truncation: a result that reaches the limit prints a warning
HYGIENE_LIMIT=3 run
assert "PR list at the limit: warning" "$(has 'WARNING: the PR list hit the limit' "$OUT/out")"
assert "issue list at the limit: warning" "$(has 'WARNING: the issue list hit the limit' "$OUT/out")"
run
assert "below the limit: no warning" "$(hasnt 'WARNING' "$OUT/out")"

# gh is pinned to the clone: run from another directory, the report is the same
rc=0; (cd "$OUT" && PATH="$BIN:$PATH" bash "$R/scripts/hygiene-report.sh" > "$OUT/out2" 2> "$OUT/err2") || rc=$?
assert "run from another directory: exit 0 and the same worktree section" "$([ "$rc" = 0 ] && [ "$(has 'merged-clean .*wt-merged' "$OUT/out2")" = 0 ] && echo 0 || echo 1)"

if [ "$failures" -ne 0 ]; then echo "$failures FAILED"; exit 1; fi
echo "all passed"

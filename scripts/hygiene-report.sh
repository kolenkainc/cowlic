#!/usr/bin/env bash
# Read-only repository hygiene report (#340). A manual command, never scheduled, never a gate: the exit code is 0.
#   bash scripts/hygiene-report.sh [--days N]        (N = age threshold in days, default 7)
#
# Prints five sections: (1) git worktrees, (2) local branches whose PR is merged, (3) remote branches without an
# open PR, (4) orphan test processes, (5) board tickets In progress without an open PR and older than N days.
#
# Read-only by construction: it deletes, kills, edits and writes NOTHING (no branch -d, no worktree remove, no fetch,
# no issue edit, no file written; output goes to stdout/stderr only). The only network use is read-only gh calls
# (gh auth status, gh pr list, gh api GET, gh issue list; no mutation). Needs GNU date (-d) and timeout (Linux; macOS
# is not supported). Without gh, or with an unauthenticated gh, the gh-based parts print a skip line. It never reads .env*, never prints the environment, tokens or ids. Every name
# that comes from outside (branch names, PR/issue titles, process command lines) is reduced to printable ASCII and
# cut before it is printed, and is only ever passed to commands as a quoted argument or compared as data.
# Test: scripts/test-hygiene-report.sh
set -uo pipefail

DAYS=7
while [ "$#" -gt 0 ]; do
  case "$1" in
    --days)
      if [ "$#" -ge 2 ]; then DAYS="$2"; shift; else echo "hygiene-report: --days needs a value, using 7" >&2; fi ;;
    --days=*) DAYS="${1#--days=}" ;;
    -h | --help)
      echo "usage: bash scripts/hygiene-report.sh [--days N]   (read-only report, always exits 0)"
      exit 0 ;;
    *) echo "hygiene-report: ignoring unknown argument" >&2 ;;
  esac
  shift
done
case "$DAYS" in '' | *[!0-9]* | 0*) echo "hygiene-report: --days must be a positive integer, using 7" >&2; DAYS=7 ;; esac
[ "${#DAYS}" -le 4 ] || DAYS=7

ORG="kolenkainc"
PROJECT_NUMBER=5
GH_TIMEOUT=90
LIMIT="${HYGIENE_LIMIT:-1000}"  # gh list limit (the env override is for the test); a warning is printed when a result reaches it
case "$LIMIT" in '' | *[!0-9]* | 0*) LIMIT=1000 ;; esac

# Printable ASCII only (strips control characters, escape sequences and non-ASCII such as bidi overrides), cut to 160.
san() { printf '%s' "${1:-}" | LC_ALL=C tr -cd '\040-\176' | cut -c1-160; }
now="$(date +%s)"
age_days() { # epoch -> whole days, or "?" when the epoch is not a number
  case "${1:-}" in '' | *[!0-9]*) echo "?" ;; *) echo $(((now - $1) / 86400)) ;; esac
}
short_path() { # replace $HOME by ~ for display
  local p="${1:-}"
  if [ -n "${HOME:-}" ] && [ "${p#"$HOME"/}" != "$p" ]; then p="~/${p#"$HOME"/}"; fi
  san "$p"
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2> /dev/null && pwd)" || ROOT=""
if [ -z "$ROOT" ] || ! git -C "$ROOT" rev-parse --git-dir > /dev/null 2>&1; then
  echo "hygiene-report: not inside a git repository; nothing to report"
  exit 0
fi
cd "$ROOT" || exit 0  # gh resolves {owner}/{repo} from the current directory: pin it to this clone
G=(git -C "$ROOT" --no-optional-locks)
CURRENT_TOP="$("${G[@]}" rev-parse --show-toplevel 2> /dev/null || true)"

echo "# cowlic hygiene report (read-only; age threshold ${DAYS} days; deletion is manual, see docs/hygiene.md)"

# ---- gh availability (once) ------------------------------------------------------------------------------------
GH_OK=0
GH_NOTE=""
if ! command -v gh > /dev/null 2>&1; then
  GH_NOTE="gh is not installed"
elif ! timeout "$GH_TIMEOUT" gh auth status > /dev/null 2>&1; then
  GH_NOTE="gh is not authenticated (run gh auth login)"
else
  GH_OK=1
fi
[ "$GH_OK" = 1 ] || echo "note: GitHub parts are limited or skipped: ${GH_NOTE}"

# ---- PR data, one read-only call: state, head branch, head sha of every PR ------------------------------------------
declare -A MERGED_SHA=() MERGED_BRANCH=() OPEN_BRANCH=() OPEN_PR_NUM=()
PR_OK=0
if [ "$GH_OK" = 1 ]; then
  if pr_tsv="$(timeout "$GH_TIMEOUT" gh pr list --state all --limit "$LIMIT" --json number,state,headRefName,headRefOid \
    --jq '.[] | [.state, .headRefName, .headRefOid, .number] | @tsv' 2> /dev/null)"; then
    PR_OK=1
    if [ "$(printf '%s\n' "$pr_tsv" | grep -c .)" -ge "$LIMIT" ]; then
      echo "WARNING: the PR list hit the limit of ${LIMIT}; it may be truncated and merged branches may show as 'no PR'"
    fi
    while IFS=$'\t' read -r st br sha prnum; do
      case "$st" in
        MERGED) MERGED_SHA["$sha"]=1; MERGED_BRANCH["$br"]="$sha" ;;
        OPEN) OPEN_BRANCH["$br"]=1; OPEN_PR_NUM["$prnum"]=1 ;;
      esac
    done <<< "$pr_tsv"
  else
    echo "note: gh pr list failed; PR-based classification is skipped"
  fi
fi

MASTER_REF="refs/remotes/origin/master"
HAVE_MASTER=1
"${G[@]}" rev-parse --verify --quiet "$MASTER_REF" > /dev/null 2>&1 || HAVE_MASTER=0
[ "$HAVE_MASTER" = 1 ] || echo "note: origin/master is not present locally; ancestor checks are skipped"
is_in_master() { [ "$HAVE_MASTER" = 1 ] && "${G[@]}" merge-base --is-ancestor "$1" "$MASTER_REF" 2> /dev/null; }
is_merged_pr_head() { [ "$PR_OK" = 1 ] && [ -n "${MERGED_SHA["$1"]+x}" ]; }

# ---- 1. worktrees ----------------------------------------------------------------------------------------------
echo
echo "## 1. git worktrees (merged-clean / stale / unmerged / dirty)"
echo "   merged = HEAD is in origin/master (as of the last fetch) or is the head of a merged PR"
n_wt=0; n_mc=0; n_stale=0; n_unm=0; n_dirty=0; n_other=0; n_unk=0
wt_path=""; wt_head=""; wt_branch=""; wt_flags=""; wt_idx=0
flush_wt() {
  [ -n "$wt_path" ] || return 0
  wt_idx=$((wt_idx + 1))
  local cls label tag="" dirty=0 cts age br
  br="${wt_branch#refs/heads/}"
  [ -n "$wt_branch" ] || br="(detached)"
  [ "$wt_idx" -ne 1 ] || tag=" [main checkout]"
  [ "$wt_path" != "$CURRENT_TOP" ] || tag="$tag [this one]"
  if [ -n "$wt_flags" ] && [ "${wt_flags#*bare}" != "$wt_flags" ]; then
    printf '   %-14s %s%s\n' "bare" "$(short_path "$wt_path")" "$tag"
    n_other=$((n_other + 1)); wt_path=""; return 0
  fi
  if [ ! -d "$wt_path" ]; then
    printf '   %-14s %s%s branch=%s (directory missing; git worktree prune clears the record)\n' "prunable" \
      "$(short_path "$wt_path")" "$tag" "$(san "$br")"
    n_other=$((n_other + 1)); n_wt=$((n_wt + 1)); wt_path=""; return 0
  fi
  n_wt=$((n_wt + 1))
  # Fail closed: a failing git status / git log (or an empty answer) means UNKNOWN, never clean.
  local st_out st_rc log_rc unknown=0
  st_out="$("${G[@]}" -C "$wt_path" status --porcelain --untracked-files=normal 2> /dev/null)"; st_rc=$?
  [ "$st_rc" -eq 0 ] || unknown=1
  [ -z "$st_out" ] || dirty=1
  cts="$("${G[@]}" -C "$wt_path" log -1 --format=%ct 2> /dev/null)"; log_rc=$?
  [ "$log_rc" -eq 0 ] || unknown=1
  age="$(age_days "$cts")"
  [ "$age" != "?" ] || unknown=1
  [ -n "$wt_head" ] || unknown=1
  local merged=0 why=""
  if is_in_master "$wt_head"; then merged=1; why="in origin/master"
  elif is_merged_pr_head "$wt_head"; then merged=1; why="head of a merged PR"; fi
  if [ "$unknown" = 1 ]; then
    cls="unknown"; n_unk=$((n_unk + 1)); why="git status/log failed: look by hand, do not remove"; merged=0
  elif [ "$wt_idx" -eq 1 ]; then
    cls="main"; n_other=$((n_other + 1))
  elif [ "$dirty" = 1 ]; then cls="dirty"; n_dirty=$((n_dirty + 1))
  elif [ "$merged" = 1 ]; then cls="merged-clean"; n_mc=$((n_mc + 1))
  elif [ "$age" != "?" ] && [ "$age" -ge "$DAYS" ]; then cls="stale"; n_stale=$((n_stale + 1))
  else cls="unmerged"; n_unm=$((n_unm + 1)); fi
  label="clean (no tracked/untracked changes; ignored files not counted)"
  [ "$dirty" = 0 ] || label="DIRTY (uncommitted work: do not remove blindly)"
  [ "$unknown" = 0 ] || label="state unknown"
  printf '   %-14s %s%s\n' "$cls" "$(short_path "$wt_path")" "$tag"
  printf '                  branch=%s head=%s last-commit=%sd ago %s%s\n' "$(san "$br")" "$(san "${wt_head:0:9}")" "$age" "$label" \
    "${why:+; $why}"
  wt_path=""
}
if wt_out="$("${G[@]}" worktree list --porcelain 2> /dev/null)"; then
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) flush_wt; wt_path="${line#worktree }"; wt_head=""; wt_branch=""; wt_flags="" ;;
      "HEAD "*) wt_head="${line#HEAD }" ;;
      "branch "*) wt_branch="${line#branch }" ;;
      bare | prunable* | locked* | detached) wt_flags="$wt_flags $line" ;;
    esac
  done <<< "$wt_out"
  flush_wt
  echo "   total: ${n_wt} worktrees: ${n_mc} merged-clean, ${n_stale} stale, ${n_unm} unmerged, ${n_dirty} dirty, ${n_unk} unknown, ${n_other} other"
else
  echo "   skipped: git worktree list failed"
fi

# ---- 2. local branches whose PR is merged -----------------------------------------------------------------------
echo
echo "## 2. local branches whose PR is merged"
if [ "$PR_OK" != 1 ]; then
  echo "   skipped: needs gh (${GH_NOTE:-gh pr list failed}); branches already in origin/master are still listed"
fi
n_lb=0; n_lb_total=0
checked_out=" "
while IFS= read -r line; do
  case "$line" in "branch "*) checked_out="$checked_out${line#branch refs/heads/} " ;; esac
done < <("${G[@]}" worktree list --porcelain 2> /dev/null)
while IFS=$'\t' read -r name sha; do
  [ -n "$name" ] || continue
  [ "$name" != master ] || continue
  n_lb_total=$((n_lb_total + 1))
  verdict=""
  if is_merged_pr_head "$sha"; then verdict="PR merged (tip is the PR head)"
  elif is_in_master "$sha"; then verdict="tip is in origin/master"
  elif [ "$PR_OK" = 1 ] && [ -n "${MERGED_BRANCH["$name"]+x}" ]; then verdict="PR merged but the local tip differs (extra local commits: check before deleting)"; fi
  [ -n "$verdict" ] || continue
  n_lb=$((n_lb + 1))
  inwt=""; [ "${checked_out#*" $name "}" = "$checked_out" ] || inwt=" [checked out in a worktree: remove the worktree first]"
  printf '   %s: %s%s\n' "$(san "$name")" "$verdict" "$inwt"
done < <("${G[@]}" for-each-ref --format='%(refname:short)%09%(objectname)' refs/heads 2> /dev/null)
echo "   total: ${n_lb} of ${n_lb_total} local branches (excluding master)"

# ---- 3. remote branches without an open PR ----------------------------------------------------------------------
echo
echo "## 3. remote branches without an open PR"
remote_names=""
src=""
if [ "$GH_OK" = 1 ] && remote_names="$(timeout "$GH_TIMEOUT" gh api --paginate 'repos/{owner}/{repo}/branches?per_page=100' --jq '.[] | [.name, .commit.sha] | @tsv' 2> /dev/null)"; then
  src="GitHub (gh api, read-only)"
else
  remote_names="$("${G[@]}" for-each-ref --format='%(refname:lstrip=3)%09%(objectname)' refs/remotes/origin 2> /dev/null || true)"
  src="local remote-tracking refs (last fetch; PR status unknown without gh)"
fi
echo "   source: ${src}"
n_rb=0; n_rb_total=0
while IFS=$'\t' read -r name rsha; do
  [ -n "$name" ] || continue
  case "$name" in master | HEAD) continue ;; esac
  n_rb_total=$((n_rb_total + 1))
  if [ "$PR_OK" = 1 ]; then
    [ -z "${OPEN_BRANCH["$name"]+x}" ] || continue
    case "$name" in
      gh-readonly-queue/*) note="merge queue branch (transient)" ;;
      *)
        if is_merged_pr_head "$rsha"; then note="PR merged, tip is the PR head (branch not deleted)"
        elif [ -n "${MERGED_BRANCH["$name"]+x}" ]; then note="PR merged but the remote tip differs: DO NOT DELETE (extra commits)"
        else note="no PR"; fi ;;
    esac
  else
    note="PR status unknown"
  fi
  n_rb=$((n_rb + 1))
  printf '   %s: %s\n' "$(san "$name")" "$note"
done <<< "$remote_names"
echo "   total: ${n_rb} of ${n_rb_total} remote branches (excluding master)"

# ---- 4. orphan test processes ----------------------------------------------------------------------------------
echo
echo "## 4. orphan test processes (read-only pgrep; nothing is killed)"
echo "   pattern: run-bootstrap-local test wrappers and their stub python under /tmp; wrangler dev / workerd listed for information"
n_pr=0
if command -v pgrep > /dev/null 2>&1; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    # pid, a kind label chosen by pattern, and the BASENAME of the executable only: never any argument
    clean="$(printf '%s' "$line" | LC_ALL=C tr -cd '\040-\176')"
    read -r -a w <<< "$clean"
    case "$clean" in
      *run-bootstrap-local.sh*) kind="bootstrap test wrapper" ;;
      */ingest/.venv/bin/python*) kind="stub ingest python" ;;
      *workerd*) kind="workerd" ;;
      *wrangler\ dev*) kind="wrangler dev" ;;
      *) kind="other match" ;;
    esac
    printf '   pid %s: %s (%s)\n' "$(san "${w[0]:-?}")" "$kind" "$(san "${w[1]##*/}")"
    n_pr=$((n_pr + 1))
  done < <(pgrep -af -- '/tmp/.*/scripts/run-bootstrap-local\.sh|/tmp/.*/ingest/\.venv/bin/python|workerd|wrangler dev' 2> /dev/null || true)
else
  echo "   skipped: pgrep not found"
fi
echo "   total: ${n_pr} matching processes (a running real test or dev server is not an orphan: judge by pid and age yourself)"

# ---- 5. board tickets In progress without an open PR, older than N days -------------------------------------------
echo
echo "## 5. open tickets In progress without an open PR, not updated for ${DAYS}+ days (board status of org ${ORG} project ${PROJECT_NUMBER})"
if [ "$GH_OK" != 1 ]; then
  echo "   skipped: ${GH_NOTE}"
elif [ "$PR_OK" != 1 ]; then
  echo "   skipped: needs the PR list (gh pr list failed)"
else
  # One read-only call; gh pages through the issues itself (limit 1000). The Status comes with each issue
  # (projectItems), which also covers items the project items listing was seen to lag behind on.
  if board="$(timeout "$GH_TIMEOUT" gh issue list --state open --limit "$LIMIT" --json number,updatedAt,title,projectItems,closedByPullRequestsReferences \
    --jq '.[] | [(if any(.projectItems[]?; .status.name == "In progress") then "P" else "-" end), .number, .updatedAt, ([.closedByPullRequestsReferences[]?.number | tostring] | if length == 0 then "-" else join(",") end), .title] | @tsv' 2> /dev/null)"; then
    n_t=0; n_all=0; n_issues=0
    while IFS=$'\t' read -r flag num upd prs title; do
      [ -n "$num" ] || continue
      case "$num" in *[!0-9]*) continue ;; esac
      n_issues=$((n_issues + 1))
      [ "$flag" = P ] || continue
      n_all=$((n_all + 1))
      has_open=0
      IFS=',' read -r -a pr_arr <<< "$prs"
      for p in "${pr_arr[@]}"; do
        case "$p" in "" | - | *[!0-9]*) continue ;; esac
        [ -z "${OPEN_PR_NUM["$p"]+x}" ] || has_open=1
      done
      [ "$has_open" = 0 ] || continue
      ts="$(date -d "$upd" +%s 2> /dev/null || true)"
      d="$(age_days "$ts")"
      if [ "$d" != "?" ] && [ "$d" -lt "$DAYS" ]; then continue; fi
      n_t=$((n_t + 1))
      printf '   #%s: last updated %sd ago: %s\n' "$num" "$d" "$(san "$title")"
    done <<< "$board"
    if [ "$n_issues" -ge "$LIMIT" ]; then echo "   WARNING: the issue list hit the limit of ${LIMIT}; the result may be truncated"; fi
    echo "   total: ${n_t} tickets (of ${n_all} In progress; age = days since the issue was last updated; only PRs linked as closing the issue count, a PR that says Refs does not)"
  else
    echo "   skipped: the issue query failed (see docs/hygiene.md for the command)"
  fi
fi

echo
echo "# end of report (nothing was changed)"
exit 0

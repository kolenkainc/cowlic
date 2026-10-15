# Repository hygiene report

`scripts/hygiene-report.sh` prints what has piled up in the local clone and on the board, so the owner can clean up
by hand (#340; background: every parallel agent run leaves a worktree and a branch, #294, #315). It is a **report**,
not a gate and not automation.

## Where and how to run it

On the owner's machine, from the main checkout (the repo root), as one command:

```
bash scripts/hygiene-report.sh            # threshold 7 days
bash scripts/hygiene-report.sh --days 14  # other threshold
```

- Manual only. It is not scheduled and no workflow runs it.
- Read-only: it deletes, kills, edits and writes nothing (no `git branch -d`, no `git worktree remove`, no `git fetch`,
  no issue edit, no file written). The exit code is always 0, also without `gh` or with an unauthenticated `gh`
  (the GitHub-based parts then print a skip line and the git-only parts still run).
- The only network use is read-only `gh` calls. It never prints tokens, ids or the environment, never reads `.env*`,
  and reduces every branch name, title and process line to printable ASCII before printing it.
- Requires Linux tools: GNU `date -d` and `timeout` (macOS is not supported). `gh` is optional. The script changes into
  its own clone before calling `gh`, so the repository never depends on your current directory.
- A `gh` list that reaches its limit (1000) prints a WARNING: it may be truncated.
- "In origin/master" means the local `refs/remotes/origin/master` as of your last `git fetch`: fetch yourself first
  if you want it current (the script will not).

## What it prints

1. **Worktrees** (`git worktree list --porcelain`), per worktree: branch, HEAD, last commit age and a class.
   - `merged-clean`: clean, and HEAD is in origin/master or is the head commit of a merged PR (squash merges leave
     the branch commit outside master, so the merged PR head is checked too).
   - `dirty`: uncommitted or untracked files. Never remove without looking. "Clean" means no tracked changes and no
     untracked non-ignored files: ignored files (`worker/.dev.vars`, `node_modules`) are NOT counted.
   - `unknown`: `git status` or `git log` failed in that worktree (broken `.git` file, permissions, corrupt index).
     Never treated as clean: look by hand.
   - `stale`: clean, not merged, last commit older than N days.
   - `unmerged`: clean, not merged, younger than N days (probably still in use).
   - `prunable`: the directory is gone; `git worktree prune` only drops the record. `main` is the main checkout.
2. **Local branches whose PR is merged** (tip equals a merged PR head, or tip is in origin/master). A branch whose
   PR merged but whose local tip differs is flagged: it has extra local commits.
3. **Remote branches without an open PR** (from the GitHub API; falls back to local remote-tracking refs without
   `gh`). A branch is called "PR merged, tip is the PR head" only when its tip equals the head of a merged PR; a
   branch name reused after a merge with new commits reads "remote tip differs: DO NOT DELETE".
   `gh-readonly-queue/*` are transient merge queue branches: leave them.
4. **Orphan test processes** (`pgrep -af` for `run-bootstrap-local.sh` test wrappers and their stub python under
   `/tmp`, plus `workerd` and `wrangler dev` for information). Only the pid, a kind label and the executable's
   basename are shown, never any argument. A process that is simply still running is not an orphan: check pid and age.
5. **Open tickets In progress without an open PR** and not updated for N days.

## The exact read-only GitHub commands

```
gh auth status
gh pr list --state all --limit 1000 --json number,state,headRefName,headRefOid
gh api --paginate 'repos/{owner}/{repo}/branches?per_page=100' --jq '.[] | [.name, .commit.sha] | @tsv'
gh issue list --state open --limit 1000 --json number,updatedAt,title,projectItems,closedByPullRequestsReferences
```

Section 5 reads the board Status through each issue's `projectItems` (one call, gh pages through the issues). A
ticket counts as having a PR only when an OPEN PR is linked as closing it (`Closes #n`); a PR that says `Refs #n`
does not count. The project-items listing itself can be queried (and paginated) with GraphQL when needed, for
example to cross-check the board:

```
gh api graphql --paginate -f query='query($endCursor: String) { organization(login:"kolenkainc") { projectV2(number:5) { items(first:100, after:$endCursor) { pageInfo { hasNextPage endCursor } nodes { fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } content { ... on Issue { number state updatedAt } } } } } } }'
```

Observed on 2026-10-15: `gh project item-list 5 -L 500` and this GraphQL listing both returned 211 items and
missed issues that the board showed (for example freshly added ones); the per-issue `projectItems` status does not
have that gap, which is why the script uses it. Reading a project needs the `read:project` scope on the `gh` token.

## Manual deletion checklist

Do these by hand, one at a time, after reading the report. The script never does them.

1. `git fetch origin --prune` so that "merged" is judged against the current master.
2. `dirty` worktrees: open them, commit or discard the work yourself. Skip them until then.
3. `merged-clean` worktrees: first look for ignored files with `git -C <path> status --ignored --short`, because
   `git worktree remove` (even without `--force`) also deletes ignored files such as `worker/.dev.vars` and
   `node_modules`. Skip worktrees younger than a day: a brand-new worktree at the master tip shows as merged-clean
   before its first commit. Then `git worktree remove <path>` (without `--force`).
4. `prunable` records: `git worktree prune`.
5. Local branches whose PR is merged and that are not checked out anywhere: `git branch -D <branch>` (`-D` because a
   squash merge is not an ancestor; first check the tip is the merged PR head, which the report states). Branches
   flagged "local tip differs" need a look at `git log origin/master..<branch>` first.
6. Remote branches "PR merged, tip is the PR head": `git push origin --delete <branch>` (verify the tip first with
   `git rev-parse origin/<branch>` after a fetch; "remote tip differs" branches have commits the PR did not merge:
   never delete them blindly). Branches with "no PR" may be someone's work in progress: ask before deleting. Never delete `master` or merge queue branches.
7. Orphan processes: confirm what a pid is (`ps -fp <pid>`), then stop it yourself.
8. Stale tickets: comment, close or move them on the board; do not bulk-edit.

## Test

`bash scripts/test-hygiene-report.sh` (offline: a throwaway git repo and a stub `gh` on `PATH`; not part of CI,
like the other `scripts/test-*.sh`). It checks the classes, the read-only guarantee (a snapshot of worktrees, refs,
status and files before and after), exit code 0 without `gh` and with an unauthenticated `gh`, and that hostile
branch names and control characters are neither executed nor printed.

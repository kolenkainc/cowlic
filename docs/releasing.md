# Releasing: GitVersion and GitHub Flow

## Naming

- Milestones are `Release N (vX.0.0)`. Release 3 (bootstrap of the top Last.fm artists and their albums) is semantic version `v3.0.0`, Release 4 (cover art for albums and artists, the follow-ups of the nightly new-release phase (#173, #175, #178, #238; the phase itself is merged under Release 3 and stays off until #238 is fixed), the Worker deploy automation and the Hono routing refactor) is `v4.0.0`, Release 5 (multi-user support) is `v5.0.0`, Release 6 (ingest hardening and operations, #41–#44) is `v6.0.0`.
- A release is a tag `vX.Y.Z` on a master commit plus a GitHub Release with notes.
- `release-2` is a legacy tag from before GitVersion existed. It is not a version and is ignored by the calculation (see below).

## GitHub Flow

1. Branch from an up-to-date master: `feat|fix|chore|docs|test/<slug>`.
2. Open a PR; the required checks are `changes`, `lint` and `tests` (shown as `CI / changes`, `CI / lint`, `CI / tests`; jobs of `.github/workflows/ci.yml`; `lint` and `tests` show "skipped", which counts as passed, only when nothing under `worker/` or `ci.yml` changed).
3. Squash merge to master. The PR title becomes the one commit on master, so it must be a Conventional Commit: it drives the version.
4. Master is always releasable. Every master commit is tagged automatically after its checks and (when `worker/` changed) its `ship` job (the D1 migrations and the Worker deploy, one job since #312) succeeded; see "Who tags".

## Version rules (`GitVersion.yml`)

GitVersion 6.x, `workflow: GitHubFlow/v1`, `tag-prefix: 'v'`, `commit-message-incrementing: Enabled`.

| Commit subject (squash = PR title) | Increment |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer | major |
| `feat:` / `feat(scope):` | minor |
| `fix:`, `perf:` | patch |
| anything else (`chore`, `docs`, `ci`, `test`, `refactor`, ...) | patch (the preset's branch increment) |

Decision: chore/docs/ci/test/refactor DO increment (patch), so every master commit has a distinct, ordered version. Every commit on master, including docs and chore, increments at least the patch version. There is no opt-out: `+semver: skip` / `+semver: none` only cancel a message-driven bump, not the preset's Patch increment (verified: at tag `v3.0.0` a `docs:` commit with `+semver: skip` still gives 3.0.1-1).

On a PR the version is computed from the commits of the PR branch (merged with master by GitHub), not from the PR title. The title matters after the squash merge, when it becomes the master commit message.

The calculated version is printed in the job summary of the job `version` (shown as `CI / version`) of `.github/workflows/ci.yml` on every PR, on master and in the merge queue (it is not a required check). The job `version` is read-only: it never tags, releases or writes anything (the job `release` does, see "Who tags"). To see what the next release will be BEFORE merging, read the summary of `version` on the PR (variable `MajorMinorPatch`, the tag would be `v` plus that value; on a PR it is computed from the PR branch, so the master value can differ by the commits merged meanwhile). The version is NOT embedded into the Worker or ingest.

Quirk found while verifying: with the strict `tag-prefix: 'v'`, `next-version` must also carry the prefix (`v4.0.0`); a bare `4.0.0` fails with "Failed to parse 4.0.0 into a Semantic Version".

## `next-version`, v3.0.x and v4.0.0

`next-version` is a FLOOR (verified on GitVersion.Tool 6.8.2, `workflow: GitHubFlow/v1`, in a scratch clone of master with tags `v3.0.0` to `v3.0.9`, 2026-10-15, #330): the calculated version is raised to it when the tag-based calculation gives a lower one (measured below); with `next-version: v4.0.0` and the tag `v4.0.0` already on the commit below, the leftover line did not change the result (measured: 4.0.1, 4.1.0, 5.0.0). Cases not measured: other combinations of `next-version` and tags. Printed `MajorMinorPatch` on top of `v3.0.9` (commit titles `docs:`, `feat:`, `feat!:`):

| `next-version` | `docs:` | `feat:` | `feat!:` |
|---|---|---|---|
| `v3.0.0` (before #330) | 3.0.10 | 3.1.0 | 4.0.0 |
| `v4.0.0` (this change, commit carrying the change) | 4.0.0 | 4.0.0 | 4.0.0 |

With `next-version: v4.0.0` a `chore(release):` commit on top of `v3.0.9` also computes 4.0.0. After the tag `v4.0.0` exists on a commit the leftover line does not hold the line: on top of it `docs:` gives 4.0.1, `feat:` 4.1.0, `feat!:` 5.0.0.

History. Since the release job exists (#290 part 3), the first master run after it merged tagged the first master commit that reached it as `v3.0.0` (no `v*` tag existed, so `MajorMinorPatch` was `3.0.0`; the log of the `version` job on master printed `3.0.0-104` before), and every later commit got the next version: master is on the `v3.0.x` line (`v3.0.1`, ..., `v3.0.9` so far). `v3.0.0` is the tag of the commit that merged the release stage (owner decision, option 1, 2026-10-14, #309); it does not mean "Release 3 done".

Release 4 start (#330, owner decision #333: a TAG ONLY). `next-version` is raised to `v4.0.0`, so the first master commit whose `release` run completes is tagged `v4.0.0` (normally the squash commit of the PR that carries this change, #346, although its title `chore(release): ...` is not breaking). The floor applies to every commit while no `v4.*` tag exists, so that PR is merged ALONE: nothing else is merged or enqueued until its own `release` run has created `v4.0.0` (the `release` job ends green without a tag when master has moved on, and the next commit would then take `v4.0.0`). If the tag lands on a later commit anyway: a tag is never moved or deleted by CI; if that commit is feat, fix or perf, the job also created a GitHub Release `v4.0.0` contrary to #333, and the owner decides by hand whether to delete that Release (the tag stays) and whether `v4.0.0` on that commit is acceptable; the following commits are `v4.0.1` and up. A breaking title (`type!:` or a `BREAKING CHANGE:` footer) is NOT used for this: it would also make the job `release` create a GitHub Release automatically. The GitHub Release for `v4.0.0` is cut by hand by the owner after Release 4 completes, as for `v3.0.6`. Commits merged before that PR stay on the `v3.0.x` line; the commits after it continue from `v4.0.0` (`v4.0.1`, ...). Each later milestone needs its own PR that raises `next-version` (for Release 5: `v5.0.0`), because the version otherwise moves only by commit types; leaving the line at the last milestone value is harmless.

## Who tags

The job `release` of `.github/workflows/ci.yml` tags and releases (owner decision (a), 2026-10-14, #290 part 3; it replaces the earlier rule "nothing tags automatically, no workflow has write permission"):

- It is the ONLY job with `contents: write` (the workflow stays `contents: read`). It uses the job's `GITHUB_TOKEN` through `gh` and the REST API: no PAT, no push from a checkout, no environment, no deploy secret, no third-party action. It installs and runs NO package or tool and uses no action at all (not even a checkout): the version is computed by the read-only job `version` (no write token) and handed over as validated job outputs (`sha`, `major_minor_patch`, `pre_release_label`, `branch_name`); `release` needs `version` to have succeeded and re-validates every output (sha equal to the commit, branch master, empty prerelease label, strict `X.Y.Z`), so an empty or tampered output fails closed. The tag state is read with the `matching-refs` API (an empty list means absent, no error text is parsed).
- When it runs: push to master and `workflow_dispatch` from master in this repository (never `pull_request`, `merge_group` or a fork), only after `changes` succeeded and `lint` and `tests` succeeded in the same run (skipped only when `worker_changed` is `false`; there is no aggregating `ci` job since #307) and the deploy chain (plan, ship) is done: plan succeeded and either nothing was to be deployed (`ship` skipped by design) or `ship` succeeded (its migration steps, the deploy and the health check). A failed, cancelled or rejected `ship` (also a rejected approval of a destructive migration) never releases.
- What it creates: a tag `vX.Y.Z` on the master commit for EVERY commit (X.Y.Z = GitVersion `MajorMinorPatch`, validated: branch `master`, no prerelease label, strict `X.Y.Z`, anything else fails the job). Because every commit increments at least the patch version (see above), tags are frequent (one per master commit, docs and chore included). A GitHub Release with generated notes is created only when the squash commit subject (the PR title) is `feat`, `fix`, `perf`, a breaking one (`type!:` or a `BREAKING CHANGE:` footer), and on a manual dispatch.
- Idempotent and never moving: a tag that already exists on THIS commit is a no-op (a missing Release is still created); a tag on another commit, an annotated tag, or an unreadable tag state fails the job; a tag is never moved, deleted or forced.
- If master moved on after the commit (a newer push), the job ends green WITHOUT a tag and the newer run tags its own commit; a run replaced while pending never tags. So under fast merging not every commit gets a tag. To tag by hand: `gh release create vX.Y.Z --target <sha> --generate-notes` (owner or coordinator), or dispatch CI on master for the head (that also creates the Release).
- The commit subject is data: read through the API into a shell variable, matched only, never put into a command line, expression or summary.

## Tool audit (GitVersion.Tool 6.8.2)

- Package `GitVersion.Tool` on nuget.org, authors "GitTools and Contributors", MIT, source github.com/GitTools/GitVersion (repository commit in the nuspec 555d0ee7445119b3dee9ae94933a503ea35c6d1c).
- Version 6.8.2 published 2026-07-10 (older than two weeks), listed, latest stable release of the 6.x line.
- Installed as a .NET tool with an exact version (`dotnet tool install --tool-path`), no third-party GitHub Action (`gittools/actions` is not used: one more supply-chain link for no gain).
- The runner image `ubuntu-24.04` ships .NET SDK 8, 9 and 10 (runner-images Ubuntu2404 readme, image 20261004.327.1), so no `setup-dotnet` is needed. The workflow runs on `ubuntu-24.04`, not `-latest`, so the audited image does not change under it.
- GitVersion.Tool runs ONLY in the job `version` (read-only: no permissions block, workflow `contents: read`, no write token, no secret); there only `actions/checkout` (same pinned SHA as the other jobs of `ci.yml`) is used as an action. Never upgrade the tool without confirmation.
- Residual risk: the NuGet package is not hash-locked; the version pin is the control. The package now runs in a job without a write token; the job that holds `contents: write` (`release`) runs no third-party package and no action. A compromised package could still lie about the version, which is why `release` re-validates the outputs; the worst case is a wrong but well-formed tag name, never a moved tag.

## Follow-up question for the owner

Should the Worker report the version (for example Subsonic `serverVersion` or a `/health` field) and should ingest log it? Not decided here.

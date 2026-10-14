# Releasing: GitVersion and GitHub Flow

## Naming

- Milestones are `Release N (vX.0.0)`. Release 3 (bootstrap of the top Last.fm artists and their albums) is semantic version `v3.0.0`, Release 4 (cover art for albums and artists, the follow-ups of the nightly new-release phase (#173, #175, #178, #238; the phase itself is merged under Release 3 and stays off until #238 is fixed), the Worker deploy automation and the Hono routing refactor) is `v4.0.0`, Release 5 (multi-user support) is `v5.0.0`.
- A release is a tag `vX.Y.Z` on a master commit plus a GitHub Release with notes.
- `release-2` is a legacy tag from before GitVersion existed. It is not a version and is ignored by the calculation (see below).

## GitHub Flow

1. Branch from an up-to-date master: `feat|fix|chore|docs|test/<slug>`.
2. Open a PR; the required check is `ci` (shown as `CI / ci`; the gate job of `.github/workflows/ci.yml`).
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

The calculated version is printed in the job summary of the job `version` (shown as `CI / version`) of `.github/workflows/ci.yml` on every PR, on master and in the merge queue (it is not part of the gate `ci`). The job `version` is read-only: it never tags, releases or writes anything (the job `release` does, see "Who tags"). To see what the next release will be BEFORE merging, read the summary of `version` on the PR (variable `MajorMinorPatch`, the tag would be `v` plus that value; on a PR it is computed from the PR branch, so the master value can differ by the commits merged meanwhile). The version is NOT embedded into the Worker or ingest.

Quirk found while verifying: with the strict `tag-prefix: 'v'`, `next-version` must also carry the prefix (`v3.0.0`); a bare `3.0.0` fails with "Failed to parse 3.0.0 into a Semantic Version".

## `next-version` and v3.0.0

`next-version: v3.0.0` makes master compute 3.0.0-based versions before any `v*` tag exists. It is meant for intermittent use: in the PR that follows the first `v3.0.0` tag, delete that line from `GitVersion.yml`.

Since the release job exists (#290 part 3), the first master run after it merges tags the FIRST master commit that reaches it as `v3.0.0` (no `v*` tag exists, so `MajorMinorPatch` is `3.0.0`; the log of the `version` job on master printed `3.0.0-104` before, i.e. `MajorMinorPatch` 3.0.0 with 104 commits since the start), and every later commit gets the next version (`v3.0.1`, `v3.0.2`, ...; a `feat` commit bumps the minor, a breaking one the major). So `v3.0.0` no longer marks the end of Release 3 by itself: it is the tag of the commit that merged the release stage. If the owner wants `v3.0.0` to mean "bootstrap done", change `next-version` BEFORE merging the release PR (for example to `v2.1.0`, so the first tag is `v2.1.0`), and bump it to `v3.0.0` in the PR that completes Release 3. Deleting the line after the first tag is optional and harmless.

Owner decision (option 1, 2026-10-14): `v3.0.0` is accepted as the tag of the squash commit of the release-stage PR (#309); it does not mean "Release 3 done". For Release 4 (`v4.0.0`) raise `next-version` in `GitVersion.yml` to `v4.0.0` in the PR that starts it (a later PR is needed for each milestone: the version otherwise only moves by commit types).

## Who tags

The job `release` of `.github/workflows/ci.yml` tags and releases (owner decision (a), 2026-10-14, #290 part 3; it replaces the earlier rule "nothing tags automatically, no workflow has write permission"):

- It is the ONLY job with `contents: write` (the workflow stays `contents: read`). It uses the job's `GITHUB_TOKEN` through `gh` and the REST API: no PAT, no push from a checkout, no environment, no deploy secret, no third-party action. It installs and runs NO package or tool and uses no action at all (not even a checkout): the version is computed by the read-only job `version` (no write token) and handed over as validated job outputs (`sha`, `major_minor_patch`, `pre_release_label`, `branch_name`); `release` needs `version` to have succeeded and re-validates every output (sha equal to the commit, branch master, empty prerelease label, strict `X.Y.Z`), so an empty or tampered output fails closed. The tag state is read with the `matching-refs` API (an empty list means absent, no error text is parsed).
- When it runs: push to master and `workflow_dispatch` from master in this repository (never `pull_request`, `merge_group` or a fork), only after the gate `ci` succeeded and the deploy chain (plan, ship) is done: plan succeeded and either nothing was to be deployed (`ship` skipped by design) or `ship` succeeded (its migration steps, the deploy and the health check). A failed, cancelled or rejected `ship` (also a rejected approval of a destructive migration) never releases.
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
